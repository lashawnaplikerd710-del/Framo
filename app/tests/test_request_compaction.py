import copy,json,unittest
from agent_core.deepseek import compact_model_data
from agent_core.user_messages import chinese_diagnostic

def expand(value):
    if isinstance(value,dict):
        if set(value)=={'table_columns','table_rows'}:
            return [{k:expand(v) for k,v in zip(value['table_columns'],row)} for row in value['table_rows']]
        return {k:expand(v) for k,v in value.items()}
    if isinstance(value,list):return [expand(v) for v in value]
    return value

class RequestCompactionTests(unittest.TestCase):
    def test_lossless_nested_tables_preserve_every_fact_timestamp_and_uncertainty(self):
        source={'observations':[{'frames':[{'requested_us':j*1000,'description':'真实商品结构','uncertainty':'不能确认材质','confidence':'high'} for j in range(20)],'schema_version':'1.0'} for i in range(9)]}
        before=copy.deepcopy(source);packed=compact_model_data(source)
        self.assertEqual(expand(packed),source)
        self.assertEqual(source,before)
        self.assertLess(len(json.dumps(packed)),len(json.dumps(source)))
    def test_heterogeneous_records_remain_distinct(self):
        source=[{'id':1},{'id':2,'fact':'未知'},{'id':3},{'id':4}]
        self.assertEqual(expand(compact_model_data(source)),source)
    def test_request_limit_reason_is_chinese(self):
        self.assertIn('文本超过网关上限',chinese_diagnostic('http_error: Provider HTTP status 413: text_limit_exceeded'))
