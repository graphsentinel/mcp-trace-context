// Minimal MCP server (TS SDK 1.32.1), one tool. Nothing trace-related here.
import http from 'node:http';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';

const DOWNSTREAM = process.env.DOWNSTREAM || 'http://downstream-api.oa-lab:8080';

function build() {
  const server = new McpServer({ name: 'oa-ts1', version: '0.1.0' });
  server.registerTool('fetch_item', { description: 'Fetch one item from the downstream API', inputSchema: { id: z.string() } },
    async ({ id }) => {
      const r = await fetch(`${DOWNSTREAM}/items/${encodeURIComponent(id)}`);
      return { content: [{ type: 'text', text: await r.text() }] };
    });
  return server;
}

if (process.env.MCP_TRANSPORT === 'stdio') {
  await build().connect(new StdioServerTransport());
} else {
  // Stateless Streamable HTTP, JSON responses (SDK's documented stateless pattern).
  http.createServer(async (req, res) => {
    if (req.url !== '/mcp') { res.writeHead(404).end(); return; }
    let body = '';
    for await (const chunk of req) body += chunk;
    const server = build();
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
    res.on('close', () => { transport.close(); server.close(); });
    await server.connect(transport);
    await transport.handleRequest(req, res, body ? JSON.parse(body) : undefined);
  }).listen(8000, '0.0.0.0');
}
