#!/bin/bash

# Get current month and day
MONTH=$(date +%-m)
DAY=$(date +%-d)

# Check if it's Jan, Apr, Jul, or Oct AND within first 7 days
if [[ "$MONTH" =~ ^(1|4|7|10)$ ]] && [ "$DAY" -le 7 ]; then
    /home/$USER/.local/bin/zerodha-summary
else
    echo "Skipped: Not in the first week of Jan/Apr/Jul/Oct. Current date: $(date)"
fi