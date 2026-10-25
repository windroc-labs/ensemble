import { expect, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On, RenderPropsOf } from 'claude-code'

type Row = RenderPropsOf['UserMessage']

function notice(text: string, task: Row['task']): Row {
  return { text, origin: { kind: 'task-notification' }, isExpanded: false, task }
}

const EVENT = notice('Monitor event: "Two ticks"', { id: 'm1' })
const ENDED = notice('Monitor "Two ticks" stream ended', { id: 'm1', status: 'completed', toolUseId: 't1' })
const EXPIRED = notice('Monitor event: "Expiry test"', { id: 'm2' })
const BASH = notice('Background command "Sleep 3" completed (exit code 0)', { id: 'b1', status: 'completed', toolUseId: 't2' })
const BASH_FAILED = notice('Background command "Sleep 3" failed with exit code 1', { id: 'b2', status: 'failed', toolUseId: 't3' })
const AGENT = notice('Agent "Reply hi" finished', { id: 'a1', status: 'completed', toolUseId: 't4', durationMs: 2322 })
const PROMPT: Row = { text: 'Monitor the build', origin: { kind: 'composer' }, isExpanded: false }
const GROUP: RenderPropsOf['ToolGroup'] = {
  calls: [{ tool: 'Read', input: { file_path: '/a' }, isRunning: false, isErrored: false, isInterrupted: false }],
  isActive: false,
  isExpanded: false,
}

const MONITOR: RenderPropsOf['ToolUse'] = {
  tool_use_id: 'monitor-1',
  tool: 'Monitor',
  input: { command: 'ensemble-manager roster', description: 'Crew events with content (ensemble-manager roster)' },
  isRunning: true,
  isErrored: false,
  isInterrupted: false,
}

function engine(on: On) {
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
}

async function drawn<C extends 'UserMessage' | 'ToolGroup' | 'ToolUse'>($: Engine, component: C, props: RenderPropsOf[C]) {
  const ui = await $.ui.mount({ plugin: 'ensemble', surface: 'terminal', component, props })
  const tree = await ui.drawn()
  await ui.unmount()
  return tree
}

const HIDDEN = { type: 'Box', part: { plugin: 'ensemble' } }
const ENGINE = { type: 'Text', children: ['engine'] }

test('hides Monitor events, expiry and stream end', async ($, on) => {
  engine(on)
  for (const row of [EVENT, ENDED, EXPIRED]) expect(await drawn($, 'UserMessage', row)).toEqual(HIDDEN)
})

test('leaves other notices, the prompt, ctrl+o and folded tool lines drawn', async ($, on) => {
  engine(on)
  for (const row of [BASH, BASH_FAILED, AGENT, PROMPT, { ...EVENT, isExpanded: true }]) {
    expect(await drawn($, 'UserMessage', row)).toMatchObject(ENGINE)
  }
  expect(await drawn($, 'ToolGroup', GROUP)).toMatchObject(ENGINE)
})

test('hides Monitor tool rows while running, completed, errored, interrupted and rearmed', async ($, on) => {
  engine(on)
  for (const row of [
    MONITOR,
    { ...MONITOR, isRunning: false, output: { taskId: 'm1', timeoutMs: 1800000 } },
    { ...MONITOR, isRunning: false, isErrored: true, output: 'Monitor failed' },
    { ...MONITOR, isRunning: false, isInterrupted: true },
    { ...MONITOR, tool_use_id: 'monitor-2' },
  ]) {
    expect(await drawn($, 'ToolUse', row)).toEqual(HIDDEN)
  }
})

test('matches only the Monitor tool name, regardless of input text', async ($, on) => {
  engine(on)
  for (const tool of ['Bash', 'Read', 'Agent', 'monitor', 'MonitorStatus', 'mcp__server__Monitor']) {
    expect(await drawn($, 'ToolUse', { ...MONITOR, tool })).toMatchObject(ENGINE)
  }
  expect(await drawn($, 'ToolUse', { ...MONITOR, input: {} })).toEqual(HIDDEN)
})

test('hides crew event Bash rows with paths, prefixes, pipes and command chains', async ($, on) => {
  engine(on)
  for (const command of [
    'ensemble-crew next-event --crew c1',
    'ensemble-crew ack --crew c1 --event e1',
    '/opt/bin/ensemble-crew next-event --crew c1',
    '/opt/bin/ensemble-crew ack --crew c1 --event e1',
    'CREW=c1 ensemble-crew next-event --crew c1',
    'CREW=c1 ensemble-crew ack --crew c1 --event e1',
    'ensemble-crew next-event --crew c1 | cat',
    'cat event.json | ensemble-crew ack --crew c1 --event e1',
    'echo ready && ensemble-crew next-event --crew c1',
    'echo ready && ensemble-crew ack --crew c1 --event e1 && echo done',
  ]) {
    expect(await drawn($, 'ToolUse', { ...MONITOR, tool: 'Bash', input: { command } })).toEqual(HIDDEN)
  }
})

test('hides crew event Bash rows in every lifecycle state', async ($, on) => {
  engine(on)
  for (const command of ['ensemble-crew next-event --crew c1', 'ensemble-crew ack --crew c1 --event e1']) {
    const row = { ...MONITOR, tool: 'Bash', input: { command } }
    for (const state of [
      {},
      { isRunning: false, output: 'ok' },
      { isRunning: false, isErrored: true },
      { isRunning: false, isInterrupted: true },
    ]) {
      expect(await drawn($, 'ToolUse', { ...row, ...state })).toEqual(HIDDEN)
    }
  }
})

test('leaves unrelated Bash commands and matching input on other tools drawn', async ($, on) => {
  engine(on)
  for (const command of ['echo hello', 'ensemble-crew roster', 'ensemble-manager roster', '']) {
    expect(await drawn($, 'ToolUse', { ...MONITOR, tool: 'Bash', input: { command } })).toMatchObject(ENGINE)
  }
  for (const tool of ['Read', 'Agent', 'bash', 'mcp__server__Bash']) {
    for (const command of ['ensemble-crew next-event', 'ensemble-crew ack']) {
      expect(await drawn($, 'ToolUse', { ...MONITOR, tool, input: { command } })).toMatchObject(ENGINE)
    }
  }
})

test('leaves unexpected Bash input shapes drawn', async ($, on) => {
  engine(on)
  for (const input of [
    undefined, null, false, 42, 'ensemble-crew next-event', [], {},
    { command: undefined }, { command: null }, { command: 42 },
    { command: ['ensemble-crew ack'] }, { command: { text: 'ensemble-crew ack' } },
    { description: 'ensemble-crew next-event' },
  ]) {
    expect(await drawn($, 'ToolUse', { ...MONITOR, tool: 'Bash', input })).toMatchObject(ENGINE)
  }
})
