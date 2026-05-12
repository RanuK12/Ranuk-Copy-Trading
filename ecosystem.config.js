// PM2 ecosystem file for the Polymarket multi-strategy trading bot.
//
// Usage:
//   pm2 start ecosystem.config.js            # start
//   pm2 logs polybot                         # follow logs
//   pm2 restart polybot                      # restart
//   pm2 save && pm2 startup                  # persist across reboots
//
// Notes:
//   * `interpreter` points to the project's venv Python so PM2 uses the
//     right dependency set. Adjust the path for your machine.
//   * `max_memory_restart` guards against unbounded memory growth.
//   * `.env` is loaded by the bot itself (python-dotenv), so PM2 does not
//     need to pass secrets via `env`.

module.exports = {
  apps: [
    {
      name: "polybot",
      script: "main.py",
      interpreter: "./.venv/bin/python",
      cwd: __dirname,
      autorestart: true,
      watch: false,
      max_memory_restart: "400M",
      restart_delay: 5000,
      max_restarts: 20,
      // Forward stdout/stderr to ~/.pm2/logs/polybot-*.log
      out_file: "./logs/polybot.out.log",
      error_file: "./logs/polybot.err.log",
      merge_logs: true,
      time: true,
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
