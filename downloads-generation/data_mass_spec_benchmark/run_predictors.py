"""
"""
import argparse
import os
import signal
import sys
import time
import traceback
import math
from functools import partial

import numpy
import pandas

from mhcnames import normalize_allele_name
import tqdm  # progress bar
tqdm.monitor_interval = 0  # see https://github.com/tqdm/tqdm/issues/481

from mhcflurry.common import configure_logging
from mhcflurry.local_parallelism import (
    add_local_parallelism_args,
    worker_pool_with_gpu_assignments_from_args,
    call_wrapped_kwargs)
from mhcflurry.cluster_parallelism import (
    add_cluster_parallelism_args,
    cluster_results_from_args)


# To avoid pickling large matrices to send to child processes when running in
# parallel, we use this global variable as a place to store data. Data that is
# stored here before creating the thread pool will be inherited to the child
# processes upon fork() call, allowing us to share large data with the workers
# via shared memory.
GLOBAL_DATA = {}

parser = argparse.ArgumentParser(usage=__doc__)

parser.add_argument(
    "input_peptides",
    metavar="CSV",
    help="CSV file with 'peptide' column")
parser.add_argument(
    "--predictor",
    required=True,
    choices=("mhcflurry", "netmhcpan4"))
parser.add_argument(
    "--mhcflurry-models-dir",
    metavar="DIR",
    help="Directory to read MHCflurry models")
parser.add_argument(
    "--mhcflurry-batch-size",
    type=int,
    default=4096,
    help="Keras batch size for MHCflurry predictions. Default: %(default)s")
parser.add_argument(
    "--allele",
    default=None,
    required=True,
    nargs="+",
    help="Alleles to predict")
parser.add_argument(
    "--chunk-size",
    type=int,
    default=100000,
    help="Num peptides per job. Default: %(default)s")
parser.add_argument(
    "--out",
    metavar="DIR",
    help="Write results to DIR")
parser.add_argument(
    "--max-peptides",
    type=int,
    help="Max peptides to process. For debugging.",
    default=None)
parser.add_argument(
    "--reuse-predictions",
    metavar="DIR",
    nargs="*",
    help="Take predictions from indicated DIR instead of re-running them")

add_local_parallelism_args(parser)
add_cluster_parallelism_args(parser)

PREDICTOR_TO_COLS = {
    "mhcflurry": ["affinity"],
    "netmhcpan4": ["affinity", "percentile_rank", "elution_score"],
}


def load_results(dirname, result_df=None):
    peptides = pandas.read_csv(
        os.path.join(dirname, "peptides.csv")).peptide
    manifest_df = pandas.read_csv(os.path.join(dirname, "alleles.csv"))

    print(
        "Loading results. Existing data has",
        len(peptides),
        "peptides and",
        len(manifest_df),
        "columns")

    # Make adjustments for old style data. Can be removed later.
    if "kind" not in manifest_df.columns:
        manifest_df["kind"] = "affinity"
    if "col" not in manifest_df.columns:
        manifest_df["col"] = manifest_df.allele + " " + manifest_df.kind

    if result_df is None:
        result_df = pandas.DataFrame(
            index=peptides, columns=manifest_df.col.values, dtype="float32")
        result_df[:] = numpy.nan
        peptides_to_assign = peptides
        mask = None
    else:
        manifest_df = manifest_df.loc[manifest_df.col.isin(result_df.columns)]
        mask = (peptides.isin(result_df.index)).values
        peptides_to_assign = peptides[mask]

    print("Will load", len(peptides), "peptides and", len(manifest_df), "cols")

    for _, row in tqdm.tqdm(manifest_df.iterrows(), total=len(manifest_df)):
        with open(os.path.join(dirname, row.path), "rb") as fd:
            value = numpy.load(fd)['arr_0']
            if mask is not None:
                value = value[mask]
            result_df.loc[peptides_to_assign, row.col] = value

    return result_df


def blocks_of_ones(arr):
    """
    Given a binary matrix, return indices of rectangular blocks of 1s.

    Parameters
    ----------
    arr : binary matrix

    Returns
    -------
    List of (x1, y1, x2, y2) where all indices are INCLUSIVE. Each block spans
    from (x1, y1) on its upper left corner to (x2, y2) on its lower right corner.

    """
    arr = arr.copy()
    blocks = []
    while arr.sum() > 0:
        (x1, y1) = numpy.unravel_index(arr.argmax(), arr.shape)
        block = [x1, y1, x1, y1]

        # Extend in first dimension as far as possible
        down_stop = numpy.argmax(arr[x1:, y1] == 0) - 1
        if down_stop == -1:
            block[2] = arr.shape[0] - 1
        else:
            assert down_stop >= 0
            block[2] = x1 + down_stop

        # Extend in second dimension as far as possible
        for i in range(y1, arr.shape[1]):
            if (arr[block[0] : block[2] + 1, i] == 1).all():
                block[3] = i

        # Zero out block:
        assert (
            arr[block[0]: block[2] + 1, block[1] : block[3] + 1] == 1).all(), (arr, block)
        arr[block[0] : block[2] + 1, block[1] : block[3] + 1] = 0

        blocks.append(block)
    return blocks


def run(argv=sys.argv[1:]):
    global GLOBAL_DATA

    # On sigusr1 print stack trace
    print("To show stack trace, run:\nkill -s USR1 %d" % os.getpid())
    signal.signal(signal.SIGUSR1, lambda sig, frame: traceback.print_stack())

    args = parser.parse_args(argv)

    configure_logging()

    serial_run = not args.cluster_parallelism and args.num_jobs == 0

    alleles = [normalize_allele_name(a) for a in args.allele]
    alleles = sorted(set(alleles))

    peptides = pandas.read_csv(
        args.input_peptides, nrows=args.max_peptides).peptide.drop_duplicates()
    print("Filtering to valid peptides. Starting at: ", len(peptides))
    peptides = peptides[peptides.str.match("^[ACDEFGHIKLMNPQRSTVWY]+$")]
    print("Filtered to: ", len(peptides))
    peptides = peptides.unique()
    num_peptides = len(peptides)

    print("Predictions for %d alleles x %d peptides." % (
        len(alleles), num_peptides))

    if not os.path.exists(args.out):
        print("Creating", args.out)
        os.mkdir(args.out)

    GLOBAL_DATA["predictor"] = args.predictor
    GLOBAL_DATA["args"] = args
    GLOBAL_DATA["cols"] = PREDICTOR_TO_COLS[args.predictor]

    # Write peptide and allele lists to out dir.
    out_peptides = os.path.abspath(os.path.join(args.out, "peptides.csv"))
    pandas.DataFrame({"peptide": peptides}).to_csv(out_peptides, index=False)
    print("Wrote: ", out_peptides)

    manifest_df = []
    for allele in alleles:
        for col in PREDICTOR_TO_COLS[args.predictor]:
            manifest_df.append((allele, col))
    manifest_df = pandas.DataFrame(
        manifest_df, columns=["allele", "kind"])
    manifest_df["col"] = (
            manifest_df.allele + " " + manifest_df.kind)
    manifest_df["path"] = manifest_df.col.map(
        lambda s: s.replace("*", "").replace(" ", ".")) + ".npz"
    out_manifest = os.path.abspath(os.path.join(args.out, "alleles.csv"))
    manifest_df.to_csv(out_manifest, index=False)
    col_to_filename = manifest_df.set_index("col").path.map(
        lambda s: os.path.abspath(os.path.join(args.out, s)))
    print("Wrote: ", out_manifest)

    result_df = pandas.DataFrame(
        index=peptides, columns=manifest_df.col.values, dtype="float32")
    result_df[:] = numpy.nan

    if args.reuse_predictions:
        for dirname in args.reuse_predictions:
            if not dirname:
                continue  # ignore empty strings
            if os.path.exists(dirname):
                print("Loading predictions", dirname)
                result_df = load_results(dirname, result_df)
            else:
                print("WARNING: skipping because does not exist", dirname)

        # We rerun any alleles have nulls for any kind of values
        # (e.g. affinity, percentile rank, elution score).
        is_null_matrix = pandas.DataFrame(
            columns=alleles, index=result_df.index, dtype="int8")
        for (allele, sub_df) in manifest_df.groupby("allele"):
            is_null_matrix[allele] = result_df[sub_df.col.values].isnull().any(1)
        print("Fraction null", is_null_matrix.values.mean())

        print("Computing blocks.")
        start = time.time()
        blocks = blocks_of_ones(is_null_matrix.values)
        print("Found %d blocks in %f sec." % (
            len(blocks), (time.time() - start)))

        work_items = []
        for (row_index1, col_index1, row_index2, col_index2) in blocks:
            block_alleles = is_null_matrix.columns[col_index1 : col_index2 + 1]
            block_peptides = result_df.index[row_index1 : row_index2 + 1]

            print("Block: ", row_index1, col_index1, row_index2, col_index2)
            num_chunks = int(math.ceil(len(block_peptides) / args.chunk_size))
            print("Splitting peptides into %d chunks" % num_chunks)
            peptide_chunks = numpy.array_split(peptides, num_chunks)

            for chunk_peptides in peptide_chunks:
                work_item = {
                    'alleles': block_alleles,
                    'peptides': chunk_peptides,
                }
                work_items.append(work_item)
    else:
        # Same number of chunks for all alleles
        num_chunks = int(math.ceil(len(peptides) / args.chunk_size))
        print("Splitting peptides into %d chunks" % num_chunks)
        peptide_chunks = numpy.array_split(peptides, num_chunks)

        work_items = []
        for (_, chunk_peptides) in enumerate(peptide_chunks):
            work_item = {
                'alleles': alleles,
                'peptides': chunk_peptides,
            }
            work_items.append(work_item)
    print("Work items: ", len(work_items))

    for (i, work_item) in enumerate(work_items):
        work_item["work_item_num"] = i

    # Combine work items to form tasks.
    tasks = []
    peptides_in_last_task = None
    # We sort work_items to put small items first so they get combined.
    for work_item in sorted(work_items, key=lambda d: len(d['peptides'])):
        if peptides_in_last_task is not None and (
                len(work_item['peptides']) +
                peptides_in_last_task < args.chunk_size):

            # Add to last task.
            tasks[-1]['work_item_dicts'].append(work_item)
            peptides_in_last_task += len(work_item['peptides'])
        else:
            # New task
            tasks.append({'work_item_dicts': [work_item]})
            peptides_in_last_task = len(work_item['peptides'])

    print("Collected %d work items into %d tasks" % (
        len(work_items), len(tasks)))

    if args.predictor == "mhcflurry":
        do_predictions_function = do_predictions_mhcflurry
    else:
        do_predictions_function = do_predictions_mhctools

    worker_pool = None
    start = time.time()
    if serial_run:
        # Serial run
        print("Running in serial.")
        results = (
            do_predictions_function(**task) for task in tasks)
    elif args.cluster_parallelism:
        # Run using separate processes HPC cluster.
        print("Running on cluster.")
        results = cluster_results_from_args(
            args,
            work_function=do_predictions_function,
            work_items=tasks,
            constant_data=GLOBAL_DATA,
            input_serialization_method="dill",
            result_serialization_method="pickle",
            clear_constant_data=True)
    else:
        worker_pool = worker_pool_with_gpu_assignments_from_args(args)
        print("Worker pool", worker_pool)
        assert worker_pool is not None
        results = worker_pool.imap_unordered(
            partial(call_wrapped_kwargs, do_predictions_function),
            tasks,
            chunksize=1)

    allele_to_chunk_index_to_predictions = {}
    for allele in alleles:
        allele_to_chunk_index_to_predictions[allele] = {}

    last_write_time_per_column = dict((col, 0.0) for col in result_df.columns)

    def write_col(col):
        out_path = os.path.join(
            args.out, col_to_filename[col])
        numpy.savez(out_path, result_df[col].values)
        print(
            "Wrote [%f%% null]:" % (
                result_df[col].isnull().mean() * 100.0),
            out_path)

    for worker_results in tqdm.tqdm(results, total=len(work_items)):
        for (work_item_num, col_to_predictions) in worker_results:
            for (col, predictions) in col_to_predictions.items():
                result_df.loc[
                    work_items[work_item_num]['peptides'],
                    col
                ] = predictions
                if time.time() - last_write_time_per_column[col] > 180:
                    write_col(col)
                    last_write_time_per_column[col] = time.time()

    print("Done processing. Final write for each column.")
    for col in result_df.columns:
        write_col(col)

    if worker_pool:
        worker_pool.close()
        worker_pool.join()

    prediction_time = time.time() - start
    print("Done generating predictions in %0.2f min." % (
        prediction_time / 60.0))


def do_predictions_mhctools(work_item_dicts, constant_data=None):
    """
    Each tuple of work items consists of:

    (work_item_num, peptides, alleles)

    """

    # This may run on the cluster in a way that misses all top level imports,
    # so we have to re-import everything here.
    import time
    import numpy
    import numpy.testing
    import mhctools

    if constant_data is None:
        constant_data = GLOBAL_DATA

    cols = constant_data['cols']
    predictor_name = constant_data['args'].predictor

    results = []
    for (i, d) in enumerate(work_item_dicts):
        work_item_num = d['work_item_num']
        peptides = d['peptides']
        alleles = d['alleles']

        print("Processing work item", i + 1, "of", len(work_item_dicts))
        result = {}
        results.append((work_item_num, result))

        if predictor_name == "netmhcpan4":
            predictor = mhctools.NetMHCpan4(
                alleles=alleles, program_name="netMHCpan-4.0")
        else:
            raise ValueError("Unsupported", predictor_name)

        start = time.time()
        df = predictor.predict_peptides_dataframe(peptides)
        print("Predicted for %d peptides x %d alleles in %0.2f sec." % (
            len(peptides), len(alleles), (time.time() - start)))

        for (allele, sub_df) in df.groupby("allele"):
            for col in cols:
                result["%s %s" % (allele, col)] = (
                    sub_df[col].values.astype('float32'))
    return results


def do_predictions_mhcflurry(work_item_dicts, constant_data=None):
    """
    Each dict of work items should have keys: work_item_num, peptides, alleles

    """

    # This may run on the cluster in a way that misses all top level imports,
    # so we have to re-import everything here.
    import time
    from mhcflurry.encodable_sequences import EncodableSequences
    from mhcflurry import Class1AffinityPredictor

    if constant_data is None:
        constant_data = GLOBAL_DATA

    args = constant_data['args']

    assert args.predictor == "mhcflurry"
    assert constant_data['cols'] == ["affinity"]

    predictor = Class1AffinityPredictor.load(args.mhcflurry_models_dir)

    results = []
    for (i, d) in enumerate(work_item_dicts):
        work_item_num = d['work_item_num']
        peptides = d['peptides']
        alleles = d['alleles']

        print("Processing work item", i + 1, "of", len(work_item_dicts))
        result = {}
        results.append((work_item_num, result))
        start = time.time()
        peptides = EncodableSequences.create(peptides)
        for (i, allele) in enumerate(alleles):
            print("Processing allele %d / %d: %0.2f sec elapsed" % (
                i + 1, len(alleles), time.time() - start))
            for col in ["affinity"]:
                result["%s %s" % (allele, col)] = predictor.predict(
                    peptides=peptides,
                    allele=allele,
                    throw=False,
                    model_kwargs={
                        'batch_size': args.mhcflurry_batch_size,
                    }).astype('float32')
        print("Done predicting in", time.time() - start, "sec")
    return results


if __name__ == '__main__':
    run()