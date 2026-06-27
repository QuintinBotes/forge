import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

// `next lint` was removed in Next.js 16, so we run ESLint directly via this flat
// config. eslint-config-next@16 ships native flat-config arrays; importing
// `core-web-vitals` and `typescript` mirrors the previous
// `.eslintrc.json` extends of ["next/core-web-vitals", "next/typescript"].
const eslintConfig = [
  {
    ignores: [".next/**", "node_modules/**", "coverage/**", "next-env.d.ts"],
  },
  ...nextCoreWebVitals,
  ...nextTypescript,
];

export default eslintConfig;
