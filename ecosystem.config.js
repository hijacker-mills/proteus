// PM2 process definition for proteus.
// uvicorn itself forks WORKERS async worker processes, so PM2 manages one
// supervised entry. To scale across machines, run this on each box behind a
// shared nginx/L4 load balancer (proteus is fully stateless).
module.exports = {
  apps: [
    {
      name: "proteus",
      cwd: "/home/ubuntu/proteus",
      script: "scripts/run.sh",
      interpreter: "bash",
      autorestart: true,
      max_restarts: 10,
      kill_timeout: 8000, // allow in-flight SSE streams to drain
      env: { PYTHONUNBUFFERED: "1" },
    },
    // Singleton poller for polling-based channels (Telegram long-poll, Signal).
    // ONE instance only — do not scale. Webhook channels don't need this.
    {
      name: "proteus-channels",
      cwd: "/home/ubuntu/proteus",
      script: "scripts/run_channels.sh",
      interpreter: "bash",
      instances: 1,
      autorestart: true,
      max_restarts: 10,
      env: { PYTHONUNBUFFERED: "1" },
    },
  ],
};
