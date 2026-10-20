/** @type {import('tailwindcss').Config} */
// "Night ledger": an indigo night with one saffron light. Status colours never reuse the accent.
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        ink: {
          950: '#0B0D1E',
          900: '#111430',
          850: '#161A3B',
          800: '#1C2146',
          700: '#282E5C',
          600: '#394075',
          500: '#5A6197',
          400: '#8A8FBF',
          300: '#B3B6D8',
          200: '#D8D7EA',
          100: '#F2EEE4',
        },
        // Saffron: the single accent (primary actions, capture, active tab).
        signal: { DEFAULT: '#FFC24B', dim: '#8C6A26' },
        mint: { 400: '#5EE0A8' },
        amber: { 400: '#FF9548' },
        danger: { 400: '#FF5C7A' },
      },
      fontFamily: {
        display: ['"Bricolage Grotesque"', 'system-ui', 'sans-serif'],
        sans: ['"Atkinson Hyperlegible Next"', '"Atkinson Hyperlegible"', 'system-ui', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'monospace'],
      },
      borderRadius: {
        sheet: '28px',
        card: '20px',
        row: '14px',
      },
      transitionTimingFunction: {
        spring: 'cubic-bezier(0.34, 1.36, 0.64, 1)',
        out: 'cubic-bezier(0.22, 1, 0.36, 1)',
      },
      transitionDuration: {
        fast: '150ms',
        base: '220ms',
        slow: '300ms',
      },
    },
  },
  plugins: [],
};
