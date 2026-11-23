import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()            # the batch's worker processes of the frozen exe start here
    from sound.main import main
    sys.exit(main())
