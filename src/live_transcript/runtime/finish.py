#!/usr/bin/env python3
"live_transcript.runtime.finish.py <mjson> <pid> — mark the meeting finished, but only if this\nexact run still owns the record (a rejoin run may have replaced it: don't clobber)."

import sys, time
from live_transcript.runtime.state import update

if __name__ == "__main__":
    update(sys.argv[1], int(sys.argv[2]), dict(status="finished", finished=time.time()))
