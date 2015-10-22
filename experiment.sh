#!/bin/bash

NUM_PROCS=32

make -r extras
make -r build_ext
echo "Experiment 1 out of 1: building using $NUM_PROCS processes"
time make -r -j$NUM_PROCS experiment
