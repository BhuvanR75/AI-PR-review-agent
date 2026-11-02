/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The console is a client over the API, so every page is static until it fetches.
  output: "standalone",
};

export default nextConfig;
