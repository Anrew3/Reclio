// @ts-check
// Minimal Docusaurus config. Everything lives under docs/docs.

const { themes: prismThemes } = require('prism-react-renderer');

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'Reclio',
  tagline: 'Netflix-style personalized recommendations for Chillio, powered by your Trakt.',
  favicon: 'img/favicon.ico',

  url: 'https://anrew3.github.io',
  baseUrl: '/Reclio/',

  organizationName: 'Anrew3',
  projectName: 'Reclio',
  deploymentBranch: 'gh-pages',
  trailingSlash: false,

  onBrokenLinks: 'warn',
  markdown: {
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          routeBasePath: '/',
          sidebarPath: require.resolve('./sidebars.js'),
          editUrl: 'https://github.com/Anrew3/reclio/tree/main/docs/',
        },
        blog: false,
        theme: {
          customCss: require.resolve('./src/css/custom.css'),
        },
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      colorMode: {
        defaultMode: 'dark',
        respectPrefersColorScheme: true,
      },
      navbar: {
        title: 'Reclio',
        items: [
          {
            type: 'docSidebar',
            sidebarId: 'docsSidebar',
            position: 'left',
            label: 'Docs',
          },
          {
            href: 'https://github.com/Anrew3/reclio',
            label: 'GitHub',
            position: 'right',
          },
        ],
      },
      footer: {
        style: 'dark',
        links: [
          {
            title: 'Docs',
            items: [
              { label: 'Introduction', to: '/' },
              { label: 'Setup', to: '/setup' },
              { label: 'Configuration', to: '/configuration' },
            ],
          },
          {
            title: 'Project',
            items: [
              { label: 'GitHub', href: 'https://github.com/Anrew3/reclio' },
              { label: 'Chillio', href: 'https://chillio.app' },
              { label: 'Public instance', href: 'https://reclio.p0xl.com' },
            ],
          },
        ],
        copyright: `Reclio · MIT license`,
      },
      prism: {
        theme: prismThemes.github,
        darkTheme: prismThemes.dracula,
        additionalLanguages: ['bash', 'yaml', 'json', 'python'],
      },
    }),
};

module.exports = config;
