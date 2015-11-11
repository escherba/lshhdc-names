#!/bin/bash

NUM_PROCS=`python -c "import multiprocessing as m; print m.cpu_count()"`

make -r extras
make -r build_ext
echo "Experiment 1 out of 1: running with $NUM_PROCS processes"
EXPERIMENT=study-preds/exp-20151111045709 make -r -j$NUM_PROCS experiment
