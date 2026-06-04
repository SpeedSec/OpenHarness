import type {CommandItemPayload} from './types.js';

export type CommandPaletteItem = {
	name: string;
	description: string;
	aliases: string[];
	matchedAlias?: string;
	score: number;
};

function withSlash(value: string): string {
	const trimmed = value.trim();
	if (!trimmed) {
		return '';
	}
	return trimmed.startsWith('/') ? trimmed : `/${trimmed}`;
}

function commandToken(value: string): string {
	return withSlash(value).slice(1).toLowerCase();
}

export function normalizeCommandItems(
	commands: string[],
	commandItems: CommandItemPayload[] | null | undefined,
): CommandItemPayload[] {
	const byName = new Map<string, CommandItemPayload>();

	for (const item of commandItems ?? []) {
		const name = withSlash(String(item.name ?? ''));
		if (!name) {
			continue;
		}
		byName.set(name, {
			name,
			description: item.description ?? '',
			aliases: (item.aliases ?? []).map((alias) => withSlash(String(alias))).filter(Boolean),
		});
	}

	for (const command of commands) {
		const name = withSlash(command);
		if (!name || byName.has(name)) {
			continue;
		}
		byName.set(name, {name, description: '', aliases: []});
	}

	return [...byName.values()];
}

export function filterCommandItems(
	commandItems: CommandItemPayload[],
	input: string,
	limit = 12,
): CommandPaletteItem[] {
	const trimmed = input.trimStart();
	if (!trimmed.startsWith('/')) {
		return [];
	}

	const rawQuery = trimmed.slice(1);
	if (/\s/.test(rawQuery)) {
		return [];
	}

	const query = rawQuery.toLowerCase();
	const scored = commandItems
		.map((item, index) => scoreItem(item, query, index))
		.filter((item): item is CommandPaletteItem => item !== null)
		.sort((a, b) => a.score - b.score || a.name.localeCompare(b.name));

	return scored.slice(0, limit);
}

function scoreItem(item: CommandItemPayload, query: string, index: number): CommandPaletteItem | null {
	const name = withSlash(item.name);
	if (!name) {
		return null;
	}
	const aliases = (item.aliases ?? []).map((alias) => withSlash(alias)).filter(Boolean);
	const description = item.description ?? '';
	if (!query) {
		return {name, description, aliases, score: index};
	}

	const candidates = [name, ...aliases];
	let bestScore = Number.POSITIVE_INFINITY;
	let matchedAlias: string | undefined;

	for (const candidate of candidates) {
		const token = commandToken(candidate);
		const score = scoreToken(token, query);
		if (score < bestScore) {
			bestScore = score;
			matchedAlias = candidate === name ? undefined : candidate;
		}
	}

	const descriptionIndex = description.toLowerCase().indexOf(query);
	if (descriptionIndex >= 0) {
		bestScore = Math.min(bestScore, 80 + descriptionIndex);
	}

	if (!Number.isFinite(bestScore)) {
		return null;
	}

	return {
		name,
		description,
		aliases,
		matchedAlias,
		score: bestScore + index / 1000,
	};
}

function scoreToken(token: string, query: string): number {
	if (token === query) {
		return 0;
	}
	if (token.startsWith(query)) {
		return 10 + token.length - query.length;
	}
	const segmentIndex = token
		.split(/[-_]/)
		.findIndex((segment) => segment.startsWith(query));
	if (segmentIndex >= 0) {
		return 25 + segmentIndex;
	}
	const includesIndex = token.indexOf(query);
	if (includesIndex >= 0) {
		return 35 + includesIndex;
	}
	const fuzzyScore = fuzzyMatchScore(token, query);
	if (fuzzyScore !== null) {
		return 55 + fuzzyScore;
	}
	return Number.POSITIVE_INFINITY;
}

function fuzzyMatchScore(token: string, query: string): number | null {
	let tokenIndex = 0;
	let score = 0;
	for (const char of query) {
		const foundAt = token.indexOf(char, tokenIndex);
		if (foundAt < 0) {
			return null;
		}
		score += foundAt - tokenIndex;
		tokenIndex = foundAt + 1;
	}
	return score;
}
