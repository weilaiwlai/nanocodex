// Run the pure TUI models with Node 24; React/Ink still need npm ci for a full typecheck.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {stripTypeScriptTypes} from 'node:module';
import {spawnSync} from 'node:child_process';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'nanocodex-tui-tests-'));
try {
	for (const name of ['input_model.ts', 'input_model.test.ts', 'timeline.test.ts', 'control.ts', 'control.test.ts']) {
		const code = stripTypeScriptTypes(fs.readFileSync(path.join(root, 'tui/src', name), 'utf8'));
		fs.writeFileSync(path.join(temporary, name.replace(/\.ts$/, '.js')), code);
	}
	fs.writeFileSync(path.join(temporary, 'package.json'), '{"type":"module"}');
	const tests = ['input_model.test.js', 'timeline.test.js', 'control.test.js'].map(name => path.join(temporary, name));
	const result = spawnSync(process.execPath, ['--test', '--test-isolation=none', ...tests], {stdio: 'inherit'});
	process.exitCode = result.status ?? 1;
} finally {
	fs.rmSync(temporary, {recursive: true, force: true});
}
