"""Export bundled, disabled configuration examples without contacting devices."""
import argparse
from importlib.resources import files
from pathlib import Path
import shutil


def export(destination):
    destination = Path(destination)
    # An existing directory is never overwritten, including partially populated
    # configurations and symlinks to a real deployment.
    destination.mkdir(parents=True, exist_ok=False)

    def copy(source, target):
        for entry in source.iterdir():
            output = target / entry.name
            if entry.is_dir():
                output.mkdir()
                copy(entry, output)
            else:
                with entry.open('rb') as src, output.open('xb') as dst:
                    shutil.copyfileobj(src, dst)

    copy(files('ldcon').joinpath('resources/examples'), destination)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path, help='New directory to create')
    args = parser.parse_args(argv)
    try:
        result = export(args.destination)
    except FileExistsError:
        parser.error('Destination already exists; choose a new directory')
    print('Example configuration written to ' + str(result))
    print('All laser outputs are disabled. Review README.html before configuring hardware.')


if __name__ == '__main__':
    main()
