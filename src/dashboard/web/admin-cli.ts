/**
 * Local administration of the dashboard credential (D2), run inside the
 * dashboard container from the server console:
 *
 *   node dist/dashboard/web/admin-cli.js set-password     # asks twice, no echo
 *   node dist/dashboard/web/admin-cli.js revoke-sessions
 *   node dist/dashboard/web/admin-cli.js unlock           # clear the login throttle
 *
 * The password is read from the terminal (or, without a terminal, from the
 * first line of stdin), never from arguments or the environment, and is not
 * printed. Setting it revokes every session. There is no web bootstrap and no
 * reset by email: recovery is this command.
 */
import { hashPassword, passwordProblem } from './password.js';
import { DashboardState } from './state.js';

function readHidden(prompt: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const input = process.stdin;
    if (!input.isTTY) {
      let text = '';
      input.setEncoding('utf8');
      input.on('data', (chunk: string) => (text += chunk));
      input.on('end', () => resolve(text.split(/\r?\n/)[0] ?? ''));
      input.on('error', reject);
      return;
    }
    process.stderr.write(prompt);
    input.setRawMode(true);
    input.setEncoding('utf8');
    let value = '';
    const onData = (chunk: string): void => {
      for (const char of chunk) {
        if (char === '\r' || char === '\n') {
          input.setRawMode(false);
          input.removeListener('data', onData);
          input.pause();
          process.stderr.write('\n');
          resolve(value);
          return;
        }
        if (char === '\u0003') {
          input.setRawMode(false);
          reject(new Error('cancelled'));
          return;
        }
        if (char === '\u007f' || char === '\b') value = value.slice(0, -1);
        else value += char;
      }
    };
    input.on('data', onData);
    input.resume();
  });
}

export async function runAdminCommand(command: string | undefined, state: DashboardState): Promise<string> {
  const now = new Date();
  switch (command) {
    case 'set-password': {
      const first = await readHidden('New dashboard password: ');
      const problem = passwordProblem(first);
      if (problem) throw new Error(`password ${problem.replace('_', ' ')}`);
      if (process.stdin.isTTY && (await readHidden('Repeat it: ')) !== first) throw new Error('passwords differ');
      state.setPassword(await hashPassword(first), now);
      return 'password set; all sessions revoked';
    }
    case 'revoke-sessions':
      state.revokeSessions(now);
      return 'all sessions revoked';
    case 'unlock':
      state.setThrottle({ failures: 0, locked_until: null });
      return 'login throttle cleared';
    default:
      throw new Error('usage: admin-cli.js set-password | revoke-sessions | unlock');
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const directory = process.env.NANOCLAW_DASHBOARD_STATE_DIR;
  runAdminCommand(process.argv[2], new DashboardState(directory ?? ''))
    .then((message) => process.stdout.write(message + '\n'))
    .catch((error: Error) => {
      process.stderr.write(`dashboard admin: ${error.message}\n`);
      process.exitCode = 1;
    });
}
