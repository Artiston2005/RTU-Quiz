/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './*.html',
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
      },
      colors: {
        'rtu-bg': '#020617',
        'rtu-surface': '#020617',
        'rtu-card': 'rgba(15,23,42,0.96)',
        'rtu-border': 'rgba(148,163,184,0.35)',
        'rtu-soft': 'rgba(15,23,42,0.85)',
        'rtu-accent': '#6366f1',
        'rtu-accent-soft': 'rgba(99,102,241,0.14)',
      },
      boxShadow: {
        'rtu-elevated': '0 24px 60px rgba(15,23,42,0.9)',
      },
      borderRadius: {
        '3xl': '1.75rem',
      },
    },
  },
  plugins: [],
};
