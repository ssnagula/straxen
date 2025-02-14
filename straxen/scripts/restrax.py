"""
Restrax: Rechunking live data
=============================================
How to use
----------------
    <activate conda environment>
    restrax --production

----------------

For more info, see the documentation:
https://straxen.readthedocs.io/en/latest/scripts.html
"""

__version__ = "0.3.1"

import argparse
import logging
import os
import typing

import immutabledict
import socket
import shutil
import time
import numpy as np
import strax
import straxen
import threading
import daqnt
import fnmatch
import typing as ty
from straxen import daq_core
from memory_profiler import memory_usage
import glob
from ast import literal_eval
from straxen.daq_core import now


def parse_args():
    parser = argparse.ArgumentParser(description="XENONnT rechunking manager")
    parser.add_argument(
        "--production",
        action="store_true",
        help="Run restrax in production mode, otherwise run in a test mode.",
    )
    parser.add_argument(
        "--ignore_checks",
        action="store_true",
        help="Do not use! Skip checks before changing documents, there be dragons.",
    )
    parser.add_argument(
        "--input_folder",
        type=str,
        default=daq_core.pre_folder,
        help="Where to read the data from, should only be used when testing",
    )
    parser.add_argument(
        "--max_threads",
        type=int,
        default=2,
        help="max number of threads to simultaneously work on one run",
    )
    parser.add_argument(
        "--skip_compression",
        nargs="*",
        default=["*event*"],
        help='skip recompression of any datatype that fnmatches. For example: "*event* *peak*"',
    )
    # deep_compare is meant only for testing
    parser.add_argument(
        "--deep_compare",
        action="store_true",
        help="Open all the data of the old and the new format and check that they are the same",
    )
    parser.add_argument(
        "--recompress_min_chunks",
        type=int,
        default=2,
        help="Only bother with doing the recompression if there are more than this many chunks",
    )
    parser.add_argument(
        "--bypass_mode",
        action="store_true",
        help="Stop recompression and just rename folders. Use with care!",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--undying", action="store_true", help="Except any error and ignore it")
    actions.add_argument("--process", type=int, help="Handle a single run")
    args = parser.parse_args()
    if args.input_folder != daq_core.pre_folder and args.production:
        raise ValueError(
            "Thou shall not pass, don't upload files from non production"
            " folders, what are you thinking you're doing?!!"
        )
    return args


def main():
    args = parse_args()
    restrax = ReStrax(args=args)

    while True:
        try:
            run_restrax(restrax, args)
            if args.process:
                break
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception as fatal_error:
            restrax.log.error(
                f"Fatal warning: ran into {fatal_error}. Trying to log error and restart ReStrax."
            )
            try:
                restrax.log_warning(
                    f"Fatal warning: ran into {fatal_error}",
                    priority="error",
                )
            except Exception as warning_error:
                restrax.error(f"Fatal warning: could not log {warning_error}")

            if not args.undying:
                raise

            restrax.log.warning("Restarting main loop after 60 seconds due to fatal error.")
            time.sleep(60)


def run_restrax(restrax, args):
    """Run the infinite loop of ReStrax."""
    restrax.infinite_loop(close=bool(args.process))


class ReStrax(daq_core.DataBases):
    """
    Restrax: rechunking the data from bootstrax and prepare it for admix/rucio
    """

    # Typing is important because of the overwrite_settings
    log: logging.Logger
    max_threads: int
    ignore_checks: bool
    skip_compression: bool
    process: ty.Union[str, int]
    deep_compare: bool
    recompress_min_chunks: int
    bypass_mode: bool

    nap_time: ty.Union[int, float] = 300  # s
    nap_time_short: ty.Union[int, float] = 5  # s

    folders: ty.Mapping = immutabledict.immutabledict(
        production_out=daq_core.output_folder, test_out="/data/test_processed"
    )

    raw_record_types: ty.Iterable = (
        "raw_records",
        "raw_records_nv",
        "raw_records_mv",
        "raw_records_he",
        "records",
        "records_nv",
        "records_mv",
    )

    exclude_modes: ty.Iterable = (
        "pmtgain",
        "pmtap",
        "exttrig",
        "noise",
        "nVeto_LASER_calibration",
        "mv_diffuserballs",
        "mv_fibres",
        "mv_darkrate",
    )

    # If a dataset is larger than this many bytes, only compare a single field (time)
    large_data_compare_threshold: ty.Union[int, float] = int(5e9)

    # If the total data rate is lower than this, use bz2 for the compression of raw records.
    is_heavy_rate_mbs: ty.Union[int, float] = 50  # MB/s

    # Default compressors / target sizes. Fn matches the datatype to the keys in the dict to get the
    target_compressor: ty.Mapping = immutabledict.immutabledict(
        {
            "peak*": "zstd",
            "_raw_record_compressor_light": "bz2",
            "_raw_record_compressor_heavy": "zstd",
            "_other": None,  # use the default compressor
        }
    )
    target_size: ty.Mapping = immutabledict.immutabledict(
        {
            "peak*": 1_500,
            "_other": 1_500,
            "_raw_records": 5_000,
        }
    )

    # Prevent using more than this much MB when doing data validation
    max_compare_buffer_mb: ty.Union[float, int] = 5000

    # Try recompressing this many times, inform the log database otherwise about failures
    max_tries: int = 5

    # Settings for rechunking
    parallel: ty.Optional[ty.Union[str, bool]] = True  # (either True, False or 'process')
    max_workers: int = 4

    # Timeout for the mailboxes responsible for saving the data (including compression!)
    _saver_timeout: ty.Union[float, int] = 3600  # s

    def __init__(self, args):
        super().__init__(production=args.production)

        self.hostname = socket.getfqdn()
        self._set_logger()
        # Get from the database unless testing
        self.read_from = None if args.production else args.input_folder
        self.write_to = (
            self.folders["production_out"] if args.production else self.folders["test_out"]
        )
        self.max_threads = args.max_threads
        self.ignore_checks = args.ignore_checks
        self.skip_compression = args.skip_compression
        self.process = args.process
        self.deep_compare = args.deep_compare
        self.recompress_min_chunks = args.recompress_min_chunks
        self.bypass_mode = args.bypass_mode
        self.overwrite_settings()

    def infinite_loop(self, close=False) -> None:
        """Core of restrax, recompress the data followed by several validation steps."""
        while True:
            self.overwrite_settings()
            run_doc = self.find_work()
            self.log.info("Start")
            if run_doc is None:
                if close:
                    return
                self.log.info("No work to do, sleep")
                self.take_a_nap()
                continue
            t0 = time.time()
            self.set_restrax_busy(run_doc)
            try:
                mem_for_doc = memory_usage(
                    (self.handle_run, (run_doc,)), max_iterations=1, interval=60
                )
            except Exception as exception:
                self.set_restrax_failed(run_doc, str(exception))
                self.log.error(f"Ran into {exception}")
                raise exception
            self.log.debug(f"Memory profiler says peak RAM usage was: {max(mem_for_doc):.1f} MB")
            self.log.debug(f"Took {(time.time() - t0) / 3600:.2f} h")
            self.set_restrax_done(run_doc, mem_for_doc)
            self.log.info("Loop done")
            if close:
                break

    def find_work(self, projection: ty.Optional[dict] = None) -> ty.Optional[dict]:
        """Get a list of documents to recompress and the associated rundoc.

        :param projection: optional, which fields from the run document to query
        :return: list of data documents, and the run-document that

        """
        if projection is None:
            projection = {k: 1 for k in "data number mode detectors rate".split()}
        if self.production:
            return self._find_production_work(projection)
        return self._find_testing_work(projection)

    def _find_testing_work(self, projection: dict) -> ty.Optional[dict]:
        """Find work from the pre-dir if we are testing."""
        folders = os.listdir(self.read_from)
        first_run = f"{int(self.process):06}"
        data_docs = []
        for folder in folders:
            if os.path.exists(os.path.join(self.write_to, folder)):
                # Don't do work twice
                continue
            if len(split := folder.split("-")) and len(split[0]) == 6:
                run_id, data_type, lineage = split

                if first_run is None:
                    first_run = run_id
                if run_id != first_run:
                    continue
                self.log.info(f"Do {folder}")
                data_docs.append(
                    {
                        "host": self.hostname,
                        "location": os.path.join(self.read_from, folder),
                        "type": data_type,
                        "linage_hash": lineage,
                    }
                )
        if not len(data_docs):
            return None
        run_doc = self.run_coll.find_one({"number": int(first_run)}, projection=projection)
        run_doc["data"] = data_docs
        return run_doc

    def _find_production_work(self, projection) -> ty.Optional[dict]:
        """Query the database for work."""
        query = {
            "status": "eb_finished_pre",
            "restrax": None,
            "bootstrax.state": "done",
            "bootstrax.host": self.hostname,
        }
        if self.process:
            query["number"] = int(self.process)
        kw = dict(sort=[("_id", -1)], projection=projection)
        run_doc = self.run_coll.find_one(query, **kw)

        if run_doc is None:
            self.log.info("No new work, looking for previously failed runs")
            # Look for work which we tried before (and has a restrax field)
            query.pop("restrax")
            query.update(
                {"restrax.n_tries": {"$lt": self.max_tries + 1}, "restrax.state": {"$ne": "done"}}
            )
            run_doc = self.run_coll.find_one(query, **kw)

        if run_doc is not None:
            # Update data field in place
            run_doc["data"] = self._get_data_docs(run_doc)
        return run_doc

    def set_restrax_busy(self, run_doc: dict) -> None:
        self.log.info(f"Update/create restrax doc")
        if not self.production:
            return
        restrax_doc = {
            "n_tries": 1 if "restrax" not in run_doc else run_doc["restrax"]["n_tries"] + 1,
            "state": "busy",
            "started_processing": now(),
            "host": self.hostname,
            "ended": None,
            "memory_usage_mb": None,
        }
        self.run_coll.update_one(
            {"_id": run_doc["_id"]},
            {"$set": {"restrax": restrax_doc}},
        )

    def set_restrax_done(self, run_doc: dict, memory_usage_mb: list) -> None:
        """Update the rundoc with the restrax state."""
        update = {
            "restrax.state": "done",
            "restrax.ended": now(),
            "restrax.memory_usage_mb": {
                "avg": np.average(memory_usage_mb),
                "max": np.max(memory_usage_mb),
            },
        }
        if self.production:
            self.run_coll.update_one(
                {"_id": run_doc["_id"]},
                {"$set": update},
            )
        self.log.debug(f'Finished {run_doc["number"]}, set to {update}')

    def set_restrax_failed(self, run_doc: dict, reason: str) -> None:
        """Update the rundoc with the restrax fail state."""

        update = {
            "restrax.state": "failed",
            "restrax.ended": now(),
            "restrax.reason": reason,
        }

        if self.production:
            self.run_coll.update_one(
                {"_id": run_doc["_id"]},
                {"$set": update},
            )

            # Perform the $inc update to increment n_tries
            result_inc = self.run_coll.update_one(
                {"_id": run_doc["_id"]}, {"$inc": {"restrax.n_tries": 1}}
            )
            self.log.debug(f"Increment update result: {result_inc.raw_result}")

        self.log.debug(f'Fail {run_doc["number"]} with {update}')
        if run_doc.get("restrax", {}).get("n_tries", 0) >= self.max_tries:
            self.log_warning(
                f'Failed too many times for {run_doc["number"]}! '
                "I stop trying, manual help is needed."
            )

    def bypass_run(self, run_doc: dict) -> None:
        """Simply move all the data associate in the rundoc to the production folder."""
        # TODO: perhaps add some heavy try-except here to not allow interruption of
        # moving data to end up with splined datasets
        self.log.warning(f'Move {run_doc["number"]} to production. DO NOT INTERRUPT!')
        for data_doc in run_doc.get("data", []):
            self._bypass_for_data_doc(data_doc)

    def _bypass_for_data_doc(self, data_doc: dict) -> None:
        source = data_doc["location"]
        dest = self.renamed_path(source)
        if os.path.exists(dest):
            # Honestly don't know how this could happen, but we have to be carefull here
            # We are dealing with single copies, so this is a tricky operation.
            # Just to be sure we are not losing the only copy, let's make a backup
            # at the unregistered folder.
            move_to = self.renamed_path(source, _move_to=daq_core.non_registered_folder)
            self._move_dir(dest, move_to)
            message = f"{dest} already exists?! Backing up to {move_to}"
            self.log.error(message)
            self.log_warning(message)
        if not os.path.exists(source):
            # Complete chaos - this should never happen!
            message = f"Trying to move {source}->{dest} but {source} does not exist?!"
            self.log.error(message)
            self.log_warning(message)
            return
        self._move_dir(source, dest)

    def handle_run(self, run_doc: dict) -> None:
        """For a given batch of data_docs of a given run, do all the rechunking steps."""
        self.log.debug("start handle_run")
        data_docs = self._get_data_docs(run_doc)
        self.log.info(f'{run_doc["number"]} -> doing {len(data_docs)}')
        # self.run_software_veto(run_doc)

        # Split the work in files that we will compress and files that will skip compression
        compress_docs = [d for d in data_docs if not self.should_skip_compression(run_doc, d)]
        skip_docs = [d for d in data_docs if self.should_skip_compression(run_doc, d)]
        self.log.debug(
            f"Compressing {len(compress_docs)} docs and skipping (i.e. move) {len(skip_docs)} docs"
        )
        assert len(compress_docs) + len(skip_docs) == len(data_docs), "one and one is three?! "

        self.rechunk_docs(run_doc, compress_docs)
        self.do_checks(compress_docs)
        if self.deep_compare:
            self.validate_data(compress_docs)

        # In the final bookkeeping we move documents, move them only if all
        # checks above are good and update the rundoc in the finalize_execute
        for move_doc in skip_docs:
            self._bypass_for_data_doc(move_doc)

        self.finalize_execute(data_docs)

        # Only remove the data that we rechunked (there are now two copies),
        # the moved data is always just a single copy
        self.remove_old_docs(compress_docs)
        self.log.info(f'{run_doc["number"]} succes')

    def _get_data_docs(self, run_doc: dict):
        """Extract data doc from rundoc and sort by largest first."""
        # Filter data documents that are only on this host
        if run_doc.get("data") is None:
            return []
        data_docs = [
            data_doc for data_doc in run_doc["data"] if data_doc.get("host") == self.hostname
        ]
        storage_backend = strax.FileSytemBackend()
        size = lambda data_doc: sum(
            chunk.get("nbytes", 0)
            for chunk in storage_backend.get_metadata(data_doc["location"]).get("chunks", [dict()])
        )
        try:
            data_docs = sorted(data_docs, key=size, reverse=True)
        except Exception as exception:
            self.set_restrax_failed(run_doc, str(exception))
            self.log.error(f"Ran into {exception}")
            raise exception
        return data_docs

    def run_software_veto(self, run_doc: dict):
        """This is where we can add a software veto for specific runs."""
        raise NotImplementedError

    def rechunk_docs(self, run_doc: dict, data_docs: ty.List[dict]) -> None:
        """For each of the data documents, rechunk/recompress/move/check the data, if multi-
        threading is allowed, use that.

        :param run_doc: run document
        :param data_docs: list of data documents. NB! Will work on ALL these documents so these data
            documents don't have to be the same as the data field in the run_doc.

        """
        if self.max_threads > 1:
            threads: ty.List[ty.Any] = []
            for next_doc in data_docs:
                self._sleep_while_n_threads_alive(threads, self.max_threads - 1)
                thread = threading.Thread(
                    target=self._rechunk_per_doc,
                    args=(
                        run_doc,
                        next_doc,
                    ),
                )
                thread.start()
                threads.append(thread)
            # Wait until all threads have finished
            self._sleep_while_n_threads_alive(threads, 0)
        else:
            for ddoc in data_docs:
                self._rechunk_per_doc(run_doc, ddoc)

    def _rechunk_per_doc(self, run_doc: dict, data_doc: dict) -> None:
        """Do the rechunking document by document."""
        dir_in = data_doc["location"]
        dir_out = self.renamed_path(dir_in)

        if not os.path.exists(dir_in):
            raise FileNotFoundError(data_doc)

        self.log.info(f"Start {dir_in} -> {dir_out}")
        compressor, target_size_mb = self.get_compressor_and_size(run_doc, data_doc)

        self.log.debug(f"Running with parallel {self.parallel} and max_workers {self.max_workers}")
        # If ever performance is a too big an issue, we can easily change
        # `strax.rechunker` to work on multiple tartes at the same time
        # using the mailbox system.
        summary = strax.rechunker(
            source_directory=dir_in,
            dest_directory=self.write_to,
            replace=False,
            compressor=compressor,
            target_size_mb=target_size_mb,
            rechunk=True,
            parallel=self.parallel,
            max_workers=self.max_workers,
            _timeout=self._saver_timeout,
        )
        self.log.info(f"{dir_out} written {summary}")

    def _sleep_while_n_threads_alive(self, threads, n):
        while sum(t.is_alive() for t in threads) > n:
            self.take_a_nap(self.nap_time_short)

    def get_compressor_and_size(
        self, run_doc: dict, data_doc: dict
    ) -> ty.Tuple[str, ty.Optional[int]]:
        """For a given data document infer the desired compressor, and target size.

        :param run_doc: run document
        :param data_doc: data document

        """
        # This is where we might do some fancy coding
        dtype = data_doc["type"]
        if dtype in self.raw_record_types:
            rate = sum(
                detector.get("avg", 100) for detector in run_doc.get("rate", {"none": {}}).values()
            )
            _go_fast = (
                rate > self.is_heavy_rate_mbs
                or run_doc.get("restrax", {}).get("n_tries", 0) > self.max_tries // 2
            )
            compressor = (
                self.target_compressor.get("_raw_record_compressor_heavy", "bz2")
                if _go_fast
                else self.target_compressor.get("_raw_record_compressor_light", "zstd")
            )
            self.log.debug(
                f"Use {compressor}, we have to go fast: {_go_fast} (rate~{rate:.1f} MB/s)"
            )
            target_size_mb = self.target_size.get("_raw_records", 5000)
        else:
            # Extract the compressor from the settings
            compressor = self._fnmatch_from_doc(self.target_compressor, dtype)
            target_size_mb = self._fnmatch_from_doc(self.target_size, dtype)
            if target_size_mb is None:
                target_size_mb = 1_500

        self.log.debug(f"Setting {dtype}: {compressor}, {target_size_mb} MB")
        if compressor == "blosc" and target_size_mb > 1_500:
            # We really want to stay away from the 2 GB limit!
            self.log_warning(
                f"blosc fails compressing > 2 GB chunks, got instructed to do {target_size_mb} MB. "
                "Forcing a lower value!"
            )
            target_size_mb = 1_500
        return compressor, target_size_mb

    @staticmethod
    def _fnmatch_from_doc(some_dict, target_key):
        for target, value in some_dict.items():
            if fnmatch.fnmatch(target, target_key):
                return value
        return some_dict.get("_other", None)

    def should_skip_compression(self, run_doc: dict, data_doc: dict) -> bool:
        """Should we skip recompressing this data? For example if the data is already so small that
        it's not worth recompressing.

        :param run_doc: run document
        :param data_doc: data document

        """

        # Skipp all when in bypass mode - don't log messages for each data_doc separately
        if self.is_in_bypass_mode:
            return True

        data_type = data_doc["type"]
        run_mode = run_doc["mode"]
        if any([m in run_mode for m in self.exclude_modes]):
            self.log.debug(f"Skip mode {run_mode}")
            return True
        if data_type == "live_data":
            return True
        if any(fnmatch.fnmatch(data_type, delete) for delete in self.skip_compression):
            self.log.debug(f"Skip {data_type} -> matches skip_compression")
            return True
        n_chunks = len(
            strax.FileSytemBackend().get_metadata(data_doc["location"]).get("chunks", [])
        )
        if n_chunks <= self.recompress_min_chunks and data_type not in self.raw_record_types:
            # no need to recompress data if it's only one chunk
            self.log.debug(f"Skip {data_type} -> only {n_chunks} chunks")
            return True
        return False

    def do_checks(self, data_docs: ty.List[dict]) -> None:
        """Open the metadata of the old and the new file to check if everything is consistent and no
        exceptions were encountered during recompression.

        :param data_docs: list of data documents to rechunk

        """
        if self.ignore_checks:
            # One does not just venture into Mar a lago!
            return
        storage_backend = strax.FileSytemBackend()
        errors = []
        for data_doc in data_docs:
            dir_in = data_doc["location"]
            dir_out = self.renamed_path(dir_in)

            if not os.path.exists(dir_in):
                errors.append(f"{dir_in} does not exists.")
            if not os.path.exists(dir_out):
                errors.append(f"{dir_out} does not exists.")
            md_in = storage_backend.get_metadata(dir_in)
            md_out = storage_backend.get_metadata(dir_out)

            # Filesize is "bonus" metadata so it may not always be there.
            # Hence the .get(filesize, True)
            if any(
                (chunk.get("n", 0) and not chunk.get("filesize", True))
                for chunk in md_out["chunks"]
            ):
                # E.g. you tried compressing >2 GB chunk using blosc
                errors.append(f"For {dir_out}, at least one doc failed to write")

            if "exception" in md_out:
                errors.append("Writing error!")

            if sum(chunk["n"] for chunk in md_in["chunks"]) != sum(
                chunk["n"] for chunk in md_out["chunks"]
            ):
                errors.append("Rechunked data has fewer entries?!")
        if errors:
            locs = [d["location"] for d in data_docs]
            raise ValueError(f"Doc {locs} had errors: " + " and ".join(errors))
        self.log.info("All checks passed")

    def validate_data(self, data_docs: ty.List[dict]):
        for data_doc in data_docs:
            self._validate_data_per_doc(data_doc)

    def _validate_data_per_doc(self, data_doc: dict) -> None:
        """Open the data from the old and new source, compare to assert it's the same."""
        storage_backend = strax.FileSytemBackend()
        dir_in = data_doc["location"]
        dir_out = self.renamed_path(dir_in)

        md_in = storage_backend.get_metadata(dir_in)
        is_large_dset = (
            sum(chunk["nbytes"] for chunk in md_in["chunks"]) > self.large_data_compare_threshold
        )

        self.log.info(f"Checking {dir_in} vs {dir_out}. Is large {is_large_dset}")
        kw = dict(
            keep_column="time" if is_large_dset else None, progress_bar=len(md_in["chunks"]) > 4
        )
        data_in = self._get_data_from_dir(dir_in, **kw)  # type: ignore
        data_out = self._get_data_from_dir(dir_out, **kw)  # type: ignore
        errors = self._compare_data(data_in, data_out)
        if errors:
            raise ValueError(
                f"Data was not the same when comparing {dir_in} {dir_out}. See:\n"
                + "\n".join(errors)
            )
        self.log.debug("Compare done")

    @staticmethod
    def _compare_data(data_in: str, data_out: str):
        """Compare structured/simple numpy arrays."""
        errors = []
        if data_in.dtype.names:
            for field in data_in.dtype.names:
                if "str" in data_in.dtype[field].name:
                    # NB! The equal_nan compare does not work for string fields!
                    if not np.array_equal(data_in[field], data_out[field]):
                        errors.append(f'Error for "{field}"')
                elif not np.array_equal(data_in[field], data_out[field], equal_nan=True):
                    errors.append(f'Error for "{field}"')
        elif not np.array_equal(data_in, data_out, equal_nan=True):
            errors.append("Data not the same")
        return errors

    def _get_data_from_dir(
        self,
        folder: str,
        keep_column: typing.Optional[ty.Union[str, list]] = None,
        progress_bar: bool = False,
        _compare_sum: bool = False,
    ) -> np.ndarray:
        """Load data from a specified folder.

        :param folder: absolute path to folder
        :param keep_column: columns to keep from structured array (if None, get all)
        :param progress_bar: show a tqdm progressbar
        :param _compare_sum: compare only the sum of each field per chunk to prevent keeping too
            much memory occoupied. Will be overwritten for too large arrays.
        :return: concatenated numpy array

        """
        meta_data = strax.FileSytemBackend()._get_metadata(folder)
        files = strax.utils.tqdm(
            sorted(glob.glob(os.path.join(folder, "*")))[:-1],
            desc=folder.split("/")[-1],
            disable=not progress_bar,
        )

        dtype = literal_eval(meta_data["dtype"])

        if keep_column:
            res_dtype = [d for d in dtype if d[0][1] in strax.to_str_tuple(keep_column)]
            if len(res_dtype) == 1:
                res_dtype = res_dtype[0][-1]
        else:
            res_dtype = dtype

        data_len = sum(d["n"] for d in meta_data["chunks"])

        if (
            np.zeros(1, dtype=res_dtype).nbytes * data_len / 1e6 > self.max_compare_buffer_mb
            or _compare_sum
        ):
            if not _compare_sum:
                self.log.info(
                    f"Memory footprint for loading {folder} would be too large, only compare sum."
                )
            # Larger than 5 GB, only computing total sum of fields
            _compare_sum = True
            # We might get integer overflows, but that shouldn't be much of a problem
            # as long as we get the same number of overflows for both datasets
            result = np.zeros(1, dtype=res_dtype)
        else:
            result = np.zeros(data_len, dtype=res_dtype)

        n_seen = 0
        for file in files:
            chunk = strax.io.load_file(file, compressor=meta_data["compressor"], dtype=dtype)

            if _compare_sum and keep_column:
                result += np.sum(chunk[keep_column])
                continue
            elif _compare_sum:
                for field in result.dtype.names:
                    result[field] += np.sum(chunk[field])
                continue

            new = chunk
            if keep_column:
                new = chunk[keep_column].copy()

            result[n_seen : n_seen + len(new)] = new
            n_seen += len(new)
        return result

    def finalize_execute(self, data_docs: ty.List[dict]) -> None:
        """Update the database according to the operation and update the metadata.

        :param data_docs: list of data documents that have been rechunk
        :return:

        """
        # Maybe could merge this with do checks? -> Avoid opening metadate twice?
        # Then again, that is SO minor in the grand scheme of things,
        # that I just leave it like this for the moment
        if not self.production or not len(data_docs):
            return
        storage_backend = strax.FileSytemBackend()
        for data_doc in data_docs:
            if self.hostname != data_doc["host"]:
                continue
            dir_out = self.renamed_path(data_doc.get("location", ""))

            new_metadata = storage_backend.get_metadata(dir_out)
            run_id = new_metadata["run_id"]
            chunk_mb = [chunk["nbytes"] / 1e6 for chunk in new_metadata["chunks"]]

            if not self.production:
                continue

            self.log.info(f"Update data doc {dir_out}")
            self.run_coll.update_one(
                {
                    "number": int(run_id),
                    "data": {
                        "$elemMatch": {"location": data_doc["location"], "host": data_doc["host"]}
                    },
                },
                {
                    "$set": {
                        "data.$.location": dir_out,
                        "data.$.file_count": len(os.listdir(dir_out)),
                        "data.$.meta.strax_version": strax.__version__,
                        "data.$.meta.straxen_version": straxen.__version__,
                        "data.$.meta.size_mb": int(np.sum(chunk_mb)),
                        "data.$.meta.avg_chunk_mb": int(np.average(chunk_mb)),
                        "data.$.meta.lineage_hash": new_metadata["lineage_hash"],
                        "data.$.meta.compressor": new_metadata["compressor"],
                    }
                },
            )
        # Mark as ready for upload such that admix can take over from here
        if self.production:
            self.run_coll.update_one(
                {"number": int(run_id)}, {"$set": {"status": "eb_ready_to_upload"}}
            )
        self.log.info("Rundoc updated")

    def remove_old_docs(self, done_data_docs: ty.List[dict]):
        for data_doc in done_data_docs:
            loc = data_doc.get("location", "??")
            assert "pre_processed" in loc
            self._remove_dir(loc)

    def take_a_nap(self, dt: ty.Optional[int] = None):
        time.sleep(dt if dt is not None else self.nap_time)

    def renamed_path(self, path: str, _move_to: ty.Optional[str] = None) -> str:
        if _move_to is None:
            _move_to = self.write_to
        return os.path.join(_move_to, os.path.split(path)[-1])

    def _set_logger(self) -> None:
        versions = straxen.print_versions(
            modules="strax straxen utilix daqnt numpy tensorflow numba".split(),
            include_git=True,
            return_string=True,
        )
        log_name = "restrax" + self.hostname + ("" if self.production else "_TESTING")
        self.log = daqnt.get_daq_logger(
            log_name,
            log_name,
            level=logging.DEBUG,
            opening_message=f"Welcome to restrax\n{versions}",
        )

    def _move_dir(self, source: str, dest: str) -> None:
        self.log.info(f"Move {source} -> {dest}")
        if self.production:
            os.rename(source, dest)

    def _remove_dir(self, directory: str) -> None:
        """Remove directory (when in production mode)"""
        self.log.info(f"Remove {directory}")
        if self.production:
            shutil.rmtree(directory)

    def log_warning(self, message: str, **kw) -> None:  # type: ignore
        self.log.warning(message)
        for key, value in dict(production=self.production, user=f"restrax_{self.hostname}").items():
            kw.setdefault(key, value)
        super().log_warning(message, **kw)

    @property
    def is_in_bypass_mode(self) -> bool:
        return self.bypass_mode

    def overwrite_settings(self):
        """Overwrite current settings with values from the database."""
        update_config = self.daq_db["restrax_config"].find_one({"name": "restrax_config"})

        type_hints = ty.get_type_hints(self.__class__)

        for field, value in update_config.items():

            if hasattr(self, field):
                current = getattr(self, field)
                if current == value:
                    continue
                current_type = type(current)

                # Match the type hinting from the class to the set value
                matches_type_hints = field in type_hints and isinstance(
                    value, self._get_instance_from_hint(type_hints[field])
                )

                is_same = isinstance(current, type(value))
                both_list = isinstance(current, tuple) and isinstance(value, list)
                both_dict = isinstance(current, immutabledict.immutabledict) and isinstance(
                    value, dict
                )
                if matches_type_hints or is_same or both_list or both_dict:
                    self.log.info(
                        f"Update self.{field} from {current} -> {value}. User"
                        f" {update_config.get('user', '??')}"
                    )
                    setattr(self, field, value)
                else:
                    self.log.warning(
                        f"Not updating {field} to {value} because the type is inconsistent "
                        f"(got {type(value)}, expected {current_type})."
                    )
        self.log.info(f"Done updating defaults (if any)")

    @staticmethod
    def _get_instance_from_hint(hint):
        """Get instances consistent with type hint.

        Searched in vain for a coherent way to typecheck generally in python 3.8.
        Python 3.11 has some nifty features (typing.reveal_type).

        Some examples:

        ```
        # Gives True
        isinstance(dict(), _get_instance_from_hint(ty.Mapping)),
        isinstance([], _get_instance_from_hint(ty.Sequence)),
        isinstance(tuple(), _get_instance_from_hint(ty.Sequence)),
        isinstance(1, _get_instance_from_hint(ty.Union[str, int])),
        isinstance(None, _get_instance_from_hint(ty.Optional[ty.Union[str, int]])),
        isinstance(1, _get_instance_from_hint(ty.Optional[ty.Union[str, int]])),

        # Gives False
        isinstance([], _get_instance_from_hint(ty.Mapping)),
        isinstance(dict(), _get_instance_from_hint(ty.Sequence)),
        isinstance(0.1, _get_instance_from_hint(ty.Sequence)),
        isinstance(0.1, _get_instance_from_hint(ty.Union[str, int])),
        isinstance(dict(), _get_instance_from_hint(ty.Optional[ty.Union[str, int]])),
        ```

        """
        if isinstance(hint, type):
            return hint

        if args := ty.get_args(hint):
            return args

        if origin := ty.get_origin(hint):
            return origin


if __name__ == "__main__":
    main()
