import { verifyRuntimeRelease } from './release-manifest.mjs';

try {
  verifyRuntimeRelease();
} catch {
  process.stderr.write('Compose release preflight failed; host startup blocked.\n');
  process.exitCode = 1;
}
