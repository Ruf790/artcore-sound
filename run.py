import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()            # the batch's worker processes of the frozen exe start here
    if "--vst-host" in sys.argv or "--vst-scan" in sys.argv:
        from sound.vst.host import main as vst_main     # 2.0: a VST3 plugin's own process (no window, no Qt)
        sys.exit(vst_main(sys.argv))
    from sound.main import main
    sys.exit(main())
