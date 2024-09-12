"""Module used to extract embeddings for samples.
"""

import argparse
import datetime
import os
import sys
from multiprocessing import Pool

import numpy as np

import analyze
import audio
import config as cfg
import model
import utils

from chirp.projects.hoplite import sqlite_impl
from chirp.projects.hoplite import interface as hoplite
from functools import partial

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))


def writeErrorLog(msg):
    with open(cfg.ERROR_LOG_FILE, "a") as elog:
        elog.write(msg + "\n")


def saveAsEmbeddingsFile(results: dict[str], fpath: str):
    """Write embeddings to file

    Args:
        results: A dictionary containing the embeddings at timestamp.
        fpath: The path for the embeddings file.
    """
    with open(fpath, "w") as f:
        for timestamp in results:
            f.write(timestamp.replace("-", "\t") + "\t" + ",".join(map(str, results[timestamp])) + "\n")


def analyzeFile(item, db: sqlite_impl.SQLiteGraphSearchDB, dataset):
    """Extracts the embeddings for a file.

    Args:
        item: (filepath, config)
    """
    # Get file path and restore cfg
    fpath: str = item[0]
    cfg.setConfig(item[1])

    offset = 0
    duration = cfg.FILE_SPLITTING_DURATION
    fileLengthSeconds = int(audio.getAudioFileLength(fpath, cfg.SAMPLE_RATE))
    results = {}

    # Start time
    start_time = datetime.datetime.now()

    # Status
    print(f"Analyzing {fpath}", flush=True)

    source_id = fpath

    # Process each chunk
    try:
        while offset < fileLengthSeconds:
            chunks = analyze.getRawAudioFromFile(fpath, offset, duration)
            start, end = offset, cfg.SIG_LENGTH + offset
            samples = []
            timestamps = []


            for c in range(len(chunks)):
                # Add to batch
                samples.append(chunks[c])
                timestamps.append([start, end])

                # Advance start and end
                start += cfg.SIG_LENGTH - cfg.SIG_OVERLAP
                end = start + cfg.SIG_LENGTH

                # Check if batch is full or last chunk
                if len(samples) < cfg.BATCH_SIZE and c < len(chunks) - 1:
                    continue

                # Prepare sample and pass through model
                data = np.array(samples, dtype="float32")
                e = model.embeddings(data)

                # Add to results
                for i in range(len(samples)):
                    # Get timestamp
                    s_start, s_end = timestamps[i]

                    # Get prediction
                    embeddings = e[i]

                    # Store embeddings
                    results[f"{s_start}-{s_end}"] = embeddings

                    embeddings_source = hoplite.EmbeddingSource(dataset, source_id, np.array([s_start, s_end]))
                    db.insert_embedding(embeddings, embeddings_source)
                    db.commit()

                # Reset batch
                samples = []
                timestamps = []

            offset = offset + duration

    except Exception as ex:
        # Write error log
        print(f"Error: Cannot analyze audio file {fpath}.", flush=True)
        utils.writeErrorLog(ex)

        return

    # Save as embeddings file
    try:
        # We have to check if output path is a file or directory
        if not cfg.OUTPUT_PATH.rsplit(".", 1)[-1].lower() in ["txt", "csv"]:
            fpath = fpath.replace(cfg.INPUT_PATH, "")
            fpath = fpath[1:] if fpath[0] in ["/", "\\"] else fpath

            # Make target directory if it doesn't exist
            fdir = os.path.join(cfg.OUTPUT_PATH, os.path.dirname(fpath))
            os.makedirs(fdir, exist_ok=True)

            saveAsEmbeddingsFile(
                results, os.path.join(cfg.OUTPUT_PATH, fpath.rsplit(".", 1)[0] + ".birdnet.embeddings.txt")
            )
        else:
            saveAsEmbeddingsFile(results, cfg.OUTPUT_PATH)

    except Exception as ex:
        # Write error log
        print(f"Error: Cannot save embeddings for {fpath}.", flush=True)
        utils.writeErrorLog(ex)

        return

    delta_time = (datetime.datetime.now() - start_time).total_seconds()
    print("Finished {} in {:.2f} seconds".format(fpath, delta_time), flush=True)

def getDatabase(db_path: str):
    """Get the database object. Creates or opens the databse.
    Args:
        db: The path to the database.
    Returns:
        The database object.
    """

    if not os.path.exists(db_path):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        db = sqlite_impl.SQLiteGraphSearchDB.create(
            db_path=db_path,
            embedding_dim=1024, #TODO dont hardcode this
        )
        db.setup()
        return db
    return sqlite_impl.SQLiteGraphSearchDB.create(db_path=db_path, embedding_dim=1024)           


if __name__ == "__main__":
    # Parse arguments
    parser = argparse.ArgumentParser(description="Extract feature embeddings with BirdNET")
    parser.add_argument(
        "--i", default="example/", help="Path to input file or folder. If this is a file, --o needs to be a file too."
    )
    parser.add_argument(
        "--o", default="example/", help="Path to output file or folder. If this is a file, --i needs to be a file too."
    )
    parser.add_argument(
        "--overlap",
        type=float,
        default=0.0,
        help="Overlap of prediction segments. Values in [0.0, 2.9]. Defaults to 0.0.",
    )
    parser.add_argument("--threads", type=int, default=4, help="Number of CPU threads.")
    parser.add_argument(
        "--batchsize", type=int, default=1, help="Number of samples to process at the same time. Defaults to 1."
    )
    parser.add_argument(
        "--fmin",
        type=int,
        default=cfg.SIG_FMIN,
        help="Minimum frequency for bandpass filter in Hz. Defaults to {} Hz.".format(cfg.SIG_FMIN),
    )
    parser.add_argument(
        "--fmax",
        type=int,
        default=cfg.SIG_FMAX,
        help="Maximum frequency for bandpass filter in Hz. Defaults to {} Hz.".format(cfg.SIG_FMAX),
    )

    # Arguments for the embeddings Database
    parser.add_argument(
        "--db",
        default="example/hoplite-db/db.sqlite",
        help="Path to the Hoplite database. Defaults to example/hoplite-db/db.sqlite.",
    )
    parser.add_argument(
        "--dataset",
        default="example",
        help="Name of the dataset. Defaults to 'example'.",
    )

    args = parser.parse_args()

    # Set paths relative to script path (requested in #3)
    cfg.MODEL_PATH = os.path.join(SCRIPT_DIR, cfg.MODEL_PATH)
    cfg.ERROR_LOG_FILE = os.path.join(SCRIPT_DIR, cfg.ERROR_LOG_FILE)

    ### Make sure to comment out appropriately if you are not using args. ###

    # Set input and output path
    cfg.INPUT_PATH = args.i
    cfg.OUTPUT_PATH = args.o

    # Parse input files
    if os.path.isdir(cfg.INPUT_PATH):
        cfg.FILE_LIST = utils.collect_audio_files(cfg.INPUT_PATH)
    else:
        cfg.FILE_LIST = [cfg.INPUT_PATH]

    # Set overlap
    cfg.SIG_OVERLAP = max(0.0, min(2.9, float(args.overlap)))

    # Set bandpass frequency range
    cfg.BANDPASS_FMIN = max(0, min(cfg.SIG_FMAX, int(args.fmin)))
    cfg.BANDPASS_FMAX = max(cfg.SIG_FMIN, min(cfg.SIG_FMAX, int(args.fmax)))

    # Set number of threads
    if os.path.isdir(cfg.INPUT_PATH):
        cfg.CPU_THREADS = max(1, int(args.threads))
        cfg.TFLITE_THREADS = 1
    else:
        cfg.CPU_THREADS = 1
        cfg.TFLITE_THREADS = max(1, int(args.threads))

    cfg.CPU_THREADS = 1 # with the current implementation, we can't use more than 1 thread

    # Set batch size
    cfg.BATCH_SIZE = max(1, int(args.batchsize))

    # Add config items to each file list entry.
    # We have to do this for Windows which does not
    # support fork() and thus each process has to
    # have its own config. USE LINUX!
    flist = [(f, cfg.getConfig()) for f in cfg.FILE_LIST]

    db = getDatabase(args.db)
    dataset = args.dataset

    # Analyze files
    if cfg.CPU_THREADS < 2:
        for entry in flist:
            analyzeFile(entry, db, dataset)
    else:
        with Pool(cfg.CPU_THREADS) as p:
            p.map(partial(analyzeFile, db=db, dataset=dataset), flist)

    # A few examples to test
    # python3 embeddings.py --i example/ --o example/ --threads 4
    # python3 embeddings.py --i example/soundscape.wav --o example/soundscape.birdnet.embeddings.txt --threads 4
