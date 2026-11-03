// One JSONL request to the current invocation's isolated native-service pipe.
import net from 'node:net';
import fs from 'node:fs';

const endpoint = process.argv[2] ?? '';
const timeout = Number(process.argv[3] ?? '30000');
const maxBytes = 128 * 1024 * 1024;

try {
  if (!/^\\\\\.\\pipe\\codex-jianying-native-[a-f0-9]{32}$/.test(endpoint)) {
    throw new Error('Only a task-owned native-service pipe is accepted');
  }
  if (!Number.isInteger(timeout) || timeout < 100 || timeout > 120000) {
    throw new Error('Invalid request timeout');
  }
  const raw = fs.readFileSync(0);
  if (raw.length > maxBytes) throw new Error('Request exceeds 128 MiB');
  const request = JSON.parse(raw.toString('utf8'));
  if (!request || typeof request !== 'object' || Array.isArray(request)
      || request.protocolVersion !== 1 || typeof request.id !== 'string'
      || !request.id || typeof request.operation !== 'string'
      || !request.operation || !['system', 'player', 'draft', 'edit', 'job'].includes(request.domain)) {
    throw new Error('Invalid native-service request envelope');
  }
  const socket = net.createConnection(endpoint);
  let buffer = '';
  let receivedBytes = 0;
  let failed = false;
  socket.setEncoding('utf8');
  socket.setTimeout(timeout, () => socket.destroy(new Error('Native request timeout')));
  socket.on('connect', () => socket.write(JSON.stringify(request) + '\n'));
  socket.on('data', chunk => {
    receivedBytes += Buffer.byteLength(chunk, 'utf8');
    if (receivedBytes > maxBytes) socket.destroy(new Error('Response exceeds 128 MiB'));
    else buffer += chunk;
  });
  socket.on('end', () => {
    if (failed) return;
    try {
      const response = JSON.parse(buffer.trim());
      if (response.id !== request.id || response.protocolVersion !== 1) {
        throw new Error('Native response identity mismatch');
      }
      process.stdout.write(JSON.stringify(response));
    } catch (error) {
      console.error(error.message);
      process.exitCode = 2;
    }
  });
  socket.on('error', error => {
    failed = true;
    console.error(error.message);
    process.exitCode = 2;
  });
} catch (error) {
  console.error(error.message);
  process.exitCode = 2;
}
