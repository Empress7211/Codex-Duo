// Run CLI children under the unmodified, officially signed Node runtime.
// Native tools authenticate the peer, its parent and its grandparent. Keeping
// the Python observer outside those three processes preserves that contract.
const { spawn } = require('node:child_process');
const { constants } = require('node:os');

const [executable, ...args] = process.argv.slice(2);
const child = spawn(executable, args, { stdio: ['pipe', 'pipe', 'inherit'] });
const signals = ['SIGTERM', 'SIGINT', 'SIGHUP'];

child.once('spawn', () => {
  process.stdout.write(JSON.stringify({ cli_pid: child.pid }) + '\n');
  child.stdout.pipe(process.stdout);
  process.stdin.pipe(child.stdin);
});

for (const signal of signals) {
  process.on(signal, () => child.kill(signal));
}

// A closed consumer should stop the CLI just as a broken pipe did before.
process.stdout.on('error', error => {
  if (error.code === 'EPIPE') child.kill('SIGTERM');
  else throw error;
});
child.stdin.on('error', error => {
  if (error.code !== 'EPIPE' && error.code !== 'ERR_STREAM_DESTROYED') throw error;
});
child.once('error', error => {
  console.error('Codex Duo could not start the native CLI: ' + error.message);
  process.exitCode = 1;
  process.stdin.destroy();
});
child.once('close', (code, signal) => {
  process.stdin.destroy();
  if (signal) {
    // Preserve Popen's negative returncode for a signal-terminated CLI.
    process.removeAllListeners(signal);
    process.kill(process.pid, signal);
    process.exitCode = 128 + constants.signals[signal];
  } else {
    process.exitCode = code;
  }
});
