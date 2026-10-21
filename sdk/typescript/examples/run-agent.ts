import { AgentStudioClient } from '../src/index';

const client = new AgentStudioClient({ baseUrl: process.env.FUSION_AGENT_STUDIO_URL });
const run = await client.submitRun({ goal: 'Summarize README.md', agent_id: process.argv[2] ?? 'default' });
for await (const event of client.streamRun(run.run_id)) {
    console.log(event.type, event.status ?? event.span ?? '');
    if (event.type === 'done') break;
}
