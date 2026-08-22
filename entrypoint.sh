#!/bin/bash

# Ensure logs directory exists
mkdir -p /app/logs

# Print environment variables (without passwords) for debugging
echo "Starting Prop-Scraper container..."
echo "VIEWPOINT_USER: ${VIEWPOINT_USER:+***}"
echo "MONGODB_URI: ${MONGODB_URI:+***}"

# Export environment variables for cron
printenv | grep -v "no_proxy" > /etc/environment

# Update cron job to include environment variables
echo "0 */6 * * * . /etc/environment; cd /app && /usr/local/bin/python app.py >> /app/logs/cron.log 2>&1" > /etc/cron.d/scraper-cron
chmod 0644 /etc/cron.d/scraper-cron
crontab /etc/cron.d/scraper-cron

# Start cron daemon in the background
service cron start

# Keep container running and tail the cron log
echo "Cron daemon started. Scraper will run every 6 hours."
echo "View logs with: docker logs <container-name>"
echo "Or follow logs in real-time: tail -f /app/logs/cron.log"
echo ""
echo "Waiting for scheduled runs..."

# Run the scraper immediately on startup (optional - remove if you only want scheduled runs)
echo "Running initial scrape..."
/usr/local/bin/python /app/app.py

# Tail the cron log to keep container alive and show output
tail -f /app/logs/cron.log

