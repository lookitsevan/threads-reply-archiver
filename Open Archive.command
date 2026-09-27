#!/bin/bash
# Double-click: grab the latest replies right now, then open the spreadsheet.
cd "$(dirname "$0")" || exit 1
/usr/bin/python3 threads_archiver.py
open data/archive.csv
