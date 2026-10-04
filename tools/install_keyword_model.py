"""Install the official small streaming keyword model (no cloud API calls)."""
import argparse
from pathlib import Path
import shutil
import tarfile
import tempfile
from urllib.request import urlopen

NAME = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/{NAME}.tar.bz2"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("/opt/athena/models/keyword"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / "model.tar.bz2"
        with urlopen(URL, timeout=40) as response, archive.open("wb") as output:
            size = 0
            while block := response.read(65536):
                size += len(block)
                if size > 100 * 1024 * 1024:
                    raise ValueError("Model archive exceeds size limit")
                output.write(block)
        with tarfile.open(archive) as bundle:
            bundle.extractall(temporary, filter="data")
        source = Path(temporary) / NAME
        destination = args.directory.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        for name, original in {
            "encoder.int8.onnx": "encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx",
            "decoder.onnx": "decoder-epoch-13-avg-2-chunk-8-left-64.onnx",
            "joiner.int8.onnx": "joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx",
            "tokens.txt": "tokens.txt",
        }.items():
            shutil.copyfile(source / original, destination / name)
        # Official phone lexicon contains ATHENA. Read its pronunciation rather
        # than guessing which phonemes this acoustic model expects.
        pronunciation = next(line.split()[1:] for line in
            (source / "en.phone").read_text().splitlines()
            if line.split() and line.split()[0].upper() == "ATHENA")
        (destination / "keywords.txt").write_text(" ".join(pronunciation) + " @ATHENA\n")
    print(f"Installed streaming ATHENA keyword model in {destination}")


if __name__ == "__main__":
    main()
