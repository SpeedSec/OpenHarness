import assert from 'node:assert/strict';
import test from 'node:test';

import {filterCommandItems, normalizeCommandItems} from './commandPalette.js';
import type {CommandItemPayload} from './types.js';

const commands: CommandItemPayload[] = [
	{
		name: '/help',
		description: 'Show available commands',
		aliases: [],
	},
	{
		name: '/workflows',
		description: 'List, run, pause, resume, and save dynamic workflows',
		aliases: ['/wf'],
	},
	{
		name: '/permissions',
		description: 'Show or update permission mode',
		aliases: [],
	},
];

test('normalizes structured command metadata and legacy command names', () => {
	const items = normalizeCommandItems(
		['help', '/status'],
		[{name: 'help', description: 'Show help', aliases: ['h']}],
	);

	assert.deepEqual(items, [
		{name: '/help', description: 'Show help', aliases: ['/h']},
		{name: '/status', description: '', aliases: []},
	]);
});

test('shows the palette for bare slash input', () => {
	const items = filterCommandItems(commands, '/');

	assert.deepEqual(items.map((item) => item.name), ['/help', '/workflows', '/permissions']);
});

test('matches aliases but returns the canonical command name', () => {
	const items = filterCommandItems(commands, '/wf');

	assert.equal(items[0].name, '/workflows');
	assert.equal(items[0].matchedAlias, '/wf');
});

test('supports fuzzy command search', () => {
	const items = filterCommandItems(commands, '/wrkflw');

	assert.equal(items[0].name, '/workflows');
});

test('hides command palette after the command token has arguments', () => {
	assert.deepEqual(filterCommandItems(commands, '/workflows run'), []);
});
