"""HAP 单选（9/11）：内部使用稳定 key，V2 写入使用已核验名称且禁止新增选项。"""
import json
from app.adapters.errors import AdapterError

SINGLE_OPTION_TYPES = (9, 11)

def option_error(code, message):
    raise AdapterError(code, message, 'field_resolve')

def option_definitions(control):
    raw = control.get('options')
    if not isinstance(raw, list):
        option_error('hap_option_metadata_missing', '单选字段缺少选项定义，请刷新目标字段')
    result = []
    keys = set()
    labels = set()
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get('key'), str) or not item['key'] or not isinstance(item.get('value'), str) or not item['value']:
            option_error('hap_option_metadata_invalid', '目标选项定义缺少有效 key 或名称')
        deleted = item.get('isDeleted', False) in (True, 1, 'true', '1')
        if item['key'] in keys:
            option_error('hap_option_ambiguous', '目标选项 key 重复，请检查控件定义')
        keys.add(item['key'])
        if not deleted:
            if item['value'] in labels:
                option_error('hap_option_ambiguous', '目标单选名称重复，无法唯一匹配')
            labels.add(item['value'])
        result.append({'key':item['key'], 'value':item['value'], 'isDeleted':deleted})
    # key 与另一个选项的名称相同也不能猜测。
    for item in result:
        if not item['isDeleted'] and any(other['key'] != item['key'] and not other['isDeleted'] and other['value'] == item['key'] for other in result):
            option_error('hap_option_ambiguous', '目标选项 key 与其他选项名称存在歧义')
    return result

def normalize_option(value, control):
    if value is None or value == '' or value == []:
        return ''
    options = option_definitions(control)
    def resolve(text):
        matches = [o for o in options if not o['isDeleted'] and text in (o['key'], o['value'])]
        if len(matches) == 1:
            return matches[0]['key']
        if len(matches) > 1:
            option_error('hap_option_ambiguous', '选项值无法唯一匹配')
        return None
    if isinstance(value, str):
        matched = resolve(value)
        if matched is not None:
            return matched
        if value.startswith('['):
            try:
                value = json.loads(value)
            except ValueError:
                option_error('hap_option_invalid', '单选回读数组不是合法 JSON')
    if isinstance(value, list):
        if not value:
            return ''
        if len(value) != 1 or not isinstance(value[0], str):
            option_error('hap_option_invalid', '单选字段只能包含一个选项')
        value = value[0]
    if not isinstance(value, str):
        option_error('hap_option_invalid', '单选字段值必须为选项名称或 key')
    matched = resolve(value)
    if matched is None:
        option_error('hap_option_not_found', '单选值不存在或选项已删除，请核对源编码与目标选项')
    return matched

def control_snapshot(control):
    out = {k:control.get(k) for k in ('controlId','alias','type','required','controlName')}
    if control.get('type') in SINGLE_OPTION_TYPES:
        out['options'] = option_definitions(control)
    return out

def same_options(old, fresh):
    # 颜色、顺序变化不改变匹配语义；key/名称/删除状态变化必须重新审核计划。
    signature = lambda control: sorted((o['key'],o['value'],o['isDeleted']) for o in option_definitions(control))
    return signature(old) == signature(fresh)

def write_controls(values, types):
    controls = []
    for cid, value in values.items():
        control = types[cid]
        item = {'controlId':cid, 'value':value}
        if control.get('type') in SINGLE_OPTION_TYPES:
            options = option_definitions(control)
            if value:
                selected = next((o for o in options if o['key']==value and not o['isDeleted']), None)
                if selected is None:
                    option_error('hap_option_not_found', '计划中的目标选项不存在或已删除')
                item['value'] = selected['value']
            item['valueType'] = 1
        controls.append(item)
    return controls