/** @type {import('postcss-load-config').Config} */
const config = {
  plugins: {
    // Tailwind CSS v4 ships its own PostCSS plugin and handles vendor
    // prefixing internally, so autoprefixer/postcss-import are no longer needed.
    "@tailwindcss/postcss": {},
  },
};

export default config;
