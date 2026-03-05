#!/bin/bash
VENV_PYTHON=/volume1/traffic/traffic_venv/bin/python
DIR=/volume1/traffic/traffic_analyser
LOG=$DIR/logs/watchdog.log

if ! pgrep -f "web_ui.py" > /dev/null; then
    echo "$(date): web_ui.py not running, starting..." >> $LOG
    cd $DIR
    nohup $VENV_PYTHON $DIR/web_ui.py --port 5002 >> $LOG 2>&1 &
fi

if ! pgrep -f "batch.py" > /dev/null; then
    echo "$(date): batch.py not running, starting..." >> $LOG
    cd $DIR
    nohup $VENV_PYTHON $DIR/batch.py >> $LOG 2>&1 &
fi