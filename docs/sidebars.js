// @ts-check

/** @type {import('@docusaurus/plugin-content-docs').SidebarsConfig} */
const sidebars = {
  docsSidebar: [
    'intro',
    {
      type: 'category',
      label: 'Getting started',
      collapsed: false,
      items: ['setup', 'configuration'],
    },
    {
      type: 'category',
      label: 'Integrations',
      collapsed: false,
      items: ['model-integrations', 'embeddings', 'recombee'],
    },
    {
      type: 'category',
      label: 'Reference',
      collapsed: false,
      items: [
        'api-reference',
        'adaptive-sync',
        'watch-state',
        'caddy-vs-traefik',
        'troubleshooting',
      ],
    },
  ],
};

module.exports = sidebars;
