import type { Register } from 'claude-code'

export const register: Register = on => {
  on('ui.render', { component: 'ToolUse', props: { tool: 'Monitor' } }, async ($, e) => {
    const { Box } = $.ui.resolve(e)
    return <Box />
  })

  on('ui.render', { component: 'ToolUse', props: { tool: 'Bash' } }, async ($, e, next) => {
    const { input } = e.props
    if (input === null || typeof input !== 'object' || Array.isArray(input) || !('command' in input)) return next(e)
    const { command } = input
    if (typeof command !== 'string' || !(command.includes('ensemble-crew next-event') || command.includes('ensemble-crew ack'))) return next(e)
    const { Box } = $.ui.resolve(e)
    return <Box />
  })

  // Monitor rows carry no task.type; the engine's summary text is what names them.
  on('ui.render', { component: 'UserMessage', props: { origin: { kind: 'task-notification' } } }, async ($, e, next) => {
    if (e.props.isExpanded || !e.props.text.startsWith('Monitor ')) return next(e)
    const { Box } = $.ui.resolve(e)
    return <Box />
  })
}
