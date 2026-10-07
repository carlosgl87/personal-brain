"""Project-specific source view for compact memory; historical sources remain readable."""
def project_source_view(source, run, project_id):
    if not run or run.result.get('schema_version') != 'action-plan-v2':
        return (run.result.get('summary', '') if run else ''), source.raw_content
    items = [item for item in run.result.get('execution', {}).get('items', [])
             if item.get('project_id') == str(project_id) and item.get('status') in {'applied', 'completed', 'already_applied'}]
    facts, quotes = [], []
    for item in items:
        facts.append(item.get('title', ''))
        kind = 'completed_tasks' if item['type'] == 'completed_task' else item['type']
        proposed = run.result.get(kind, [])
        index = item.get('index')
        if isinstance(index, int) and 0 <= index < len(proposed):
            quote = proposed[index].get('evidence', '')
            if quote and quote in source.raw_content:
                quotes.append(quote)
    return '\n'.join(dict.fromkeys(facts)), '\n'.join(dict.fromkeys(quotes))
