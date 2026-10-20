/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        ink: {
          950: '#07090d',
          900: '#0c1017',
          850: '#11161f',
          800: '#161c27',
          700: '#222a38',
          600: '#323c4e',
          500: '#4b5669',
          400: '#7a8597',
          300: '#a5aebc',
          200: '#cdd3dc',
          100: '#e8ebf0',
        },
        signal: {
          DEFAULT: '#3ee6a8',
          dim: '#1f7a5a',
        },
        amber: { 400: '#f5b84a' },
        danger: { 400: '#ff6b6b' },
      },
      fontFamily: {
        sans: ['"IBM Plex Sans"', 'system-ui', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'monospace'],
      },
    },
  },
  plugins: [],
};
