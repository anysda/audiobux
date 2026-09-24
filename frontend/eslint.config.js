import js from '@eslint/js';
import svelte from 'eslint-plugin-svelte';
import ts from 'typescript-eslint';

export default ts.config(
  { ignores: ['dist/', 'node_modules/'] },
  js.configs.recommended,
  ts.configs.recommended,
  svelte.configs.recommended,
  {
    files: ['**/*.svelte', '**/*.svelte.ts'],
    languageOptions: { parserOptions: { parser: ts.parser } },
    rules: {
      // names are checked by svelte-check, which knows the browser and the runes
      'no-undef': 'off',
      // {' '} keeps the space between words at the edge of an {#each}, where Svelte trims it
      'svelte/no-useless-mustaches': 'off',
    },
  },
);
