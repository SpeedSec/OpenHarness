import React from 'react';
import {Box, Text} from 'ink';

import type {CommandPaletteItem} from '../commandPalette.js';

function CommandPickerInner({
	hints,
	selectedIndex,
}: {
	hints: CommandPaletteItem[];
	selectedIndex: number;
}): React.JSX.Element | null {
	if (hints.length === 0) {
		return null;
	}

	return (
		<Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1} marginBottom={0}>
			<Text dimColor bold> Commands</Text>
			{hints.map((hint, i) => {
				const isSelected = i === selectedIndex;
				return (
					<Box key={hint.name} flexDirection="column">
						<Box>
							<Text color={isSelected ? 'cyan' : undefined} bold={isSelected}>
								{isSelected ? '\u276F ' : '  '}
								{hint.name}
							</Text>
							{hint.matchedAlias ? <Text dimColor>  alias {hint.matchedAlias}</Text> : null}
							{isSelected ? <Text dimColor>  [enter]</Text> : null}
						</Box>
						{isSelected && hint.description ? (
							<Box paddingLeft={2}>
								<Text dimColor>{truncate(hint.description, 86)}</Text>
							</Box>
						) : null}
					</Box>
				);
			})}
			<Text dimColor> {'\u2191\u2193'} navigate{'  '}tab complete{'  '}{'\u23CE'} run{'  '}esc dismiss</Text>
		</Box>
	);
}

export const CommandPicker = React.memo(CommandPickerInner);

function truncate(value: string, maxLength: number): string {
	if (value.length <= maxLength) {
		return value;
	}
	return `${value.slice(0, Math.max(0, maxLength - 1))}…`;
}
