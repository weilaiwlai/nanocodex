import test from 'node:test';
import assert from 'node:assert/strict';
import type {ChildProcess} from 'node:child_process';
import {createController} from './control.js';

test('control writes explicit approval as JSONL', () => {
	const writes: string[] = [];
	const child = {stdin: {writable: true, write: (value: string) => writes.push(value)}} as unknown as ChildProcess;
	const controller = createController(child);
	controller.send({type: 'permission_response', request_id: 'r1', approved: false});
	assert.deepEqual(JSON.parse(writes[0]!), {type: 'permission_response', request_id: 'r1', approved: false});
	assert.ok(writes[0]?.endsWith('\n'));
	controller.dispose();
});

test('cancel is idempotent and disposed children receive no writes', () => {
	const writes: string[] = [];
	const child = {stdin: {writable: true, write: (value: string) => writes.push(value)}} as unknown as ChildProcess;
	const controller = createController(child);
	controller.cancel();
	controller.cancel();
	assert.equal(writes.length, 1);
	assert.equal(JSON.parse(writes[0]!).type, 'cancel');
	controller.dispose();
	controller.send({type: 'cancel'});
	assert.equal(writes.length, 1);
});
