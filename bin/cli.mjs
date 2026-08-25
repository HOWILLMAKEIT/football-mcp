#!/usr/bin/env node
/**
 * football-data-mcp — npx entry point.
 *
 * The MCP server itself is Python (src/football_mcp, stdio transport). This
 * shim resolves the installed package root and runs it with `uvx --from
 * <pkgdir>`, which builds an ephemeral, cached tool environment from the
 * bundled pyproject — no venv juggling, no global installs. stdio is passed
 * straight through, which is all an MCP client needs.
 *
 * Requirements: `uv` on PATH (https://docs.astral.sh/uv/).
 */
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const pkgDir = dirname(dirname(fileURLToPath(import.meta.url))); // bin/ -> package root

if (!existsSync(join(pkgDir, 'pyproject.toml'))) {
  console.error(
    `football-data-mcp: bundled Python project missing at ${pkgDir}; reinstall the npm package.`
  );
  process.exit(1);
}

const child = spawn('uvx', ['--from', pkgDir, 'football-data-mcp'], {
  stdio: 'inherit',
});

child.on('error', (err) => {
  if (err.code === 'ENOENT') {
    console.error(
      'football-data-mcp: `uvx` not found on PATH. Install uv first: ' +
        'https://docs.astral.sh/uv/getting-started/installation/'
    );
    process.exit(1);
  }
  throw err;
});

child.on('exit', (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 0);
});
