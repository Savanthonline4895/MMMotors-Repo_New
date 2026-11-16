import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{js,jsx}'],
    extends: [
      js.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
      parserOptions: {
        ecmaVersion: 'latest',
        ecmaFeatures: { jsx: true },
        sourceType: 'module',
      },
    },
    rules: {
      // ignoreRestSiblings: `const { _key, ...rest } = row` is how we drop a field
      'no-unused-vars': ['error', { varsIgnorePattern: '^[A-Z_]', ignoreRestSiblings: true }],
      // Hooks that live next to their provider/component; editing these files does a full reload
      'react-refresh/only-export-components': ['error', {
        allowConstantExport: true,
        allowExportNames: ['useConfirm', 'useSortable', 'useAuth', 'useBadgeTypes', 'useCustomerBadgesMap', 'useBadges', 'useDraft'],
      }],
    },
  },
])
