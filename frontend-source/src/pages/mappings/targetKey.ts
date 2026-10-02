/** 业务键必须采用映射输出的实际键；元数据只用于确认 ID/别名指向同一控件。 */
export interface TargetField {
  path: string;
  name: string;
  field_id?: string;
  alias?: string;
  write_path?: string;
}

export interface TargetPair {
  source: string;
  target: string;
}

function identifies(field: TargetField, value: string): boolean {
  return [field.path, field.field_id, field.alias, field.write_path].includes(value);
}

export function resolveTargetKey(key: string | undefined, fields: TargetField[], pairs: TargetPair[]): string | undefined {
  if (!key) return undefined;
  const mapped = [...new Set(pairs.filter(p => p.source && p.target).map(p => p.target))];
  if (mapped.includes(key)) return key;
  const matches = fields.filter(f => identifies(f, key));
  if (matches.length !== 1) return key;
  const candidates = mapped.filter(target => identifies(matches[0], target));
  return candidates.length === 1 ? candidates[0] : key;
}

export function targetKeyOptions(fields: TargetField[], pairs: TargetPair[]) {
  return [...new Set(pairs.filter(p => p.source && p.target).map(p => p.target))].map(value => {
    const matches = fields.filter(f => identifies(f, value));
    const name = matches.length === 1 ? matches[0].name : undefined;
    return { value, label: name && name !== value ? `${name}（${value}）` : value };
  });
}
