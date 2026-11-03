"""Chinese presentation of diagnostics. Original errors stay in task reports.

This module never changes recovery categories, error codes or job states.
Unknown English diagnostics get a stable reference rather than a guessed cause.
"""
import hashlib
import re


RULES = (
    (r'Model request recovery deadline reached|Gateway request outcome is uncertain|outcome_uncertain', '模型请求结果尚未确认，素材与文案检查点已保留。请检查网络后点击「从检查点继续」，软件会先查询原请求；确认丢失的文本结果可自动重新生成一次，无需修改口播或重新创建任务。'),
    (r'Montage sources must match the target aspect ratio|Source and target aspect ratios differ', '素材画幅与任务要求不同。新版直接出片可自动适配；请按原素材比例新建任务，原文件无需手动裁剪。'),
    (r'Montage source interval leaves the video stream', '剪辑选段超出了素材的有效画面范围，需要重新校验选段；原素材和检查点已保留。'),
    (r'text_limit_exceeded|Provider HTTP status 413', '提交给模型的文本超过网关上限，需精简重复分析数据后重试；原任务和素材已保留。'),
    (r'Variable frame rate is not supported', '素材采用可变帧率，请重新添加素材以自动转换固定帧率工作副本，再新建任务。'),
    (r'No single bundled font covers', '内置字体无法完整显示字幕，请补充字体或调整特殊字符。'),
    (r'Font checksum mismatch|font.*(?:fingerprint|hash).*mismatch', '字体文件校验失败，请修复内置字体库后重试。'),
    (r'Runtime differs from the saved task|Model, installation or tool version changed',
     '程序版本已更新，与此任务保存版本不同。请按原素材和要求新建任务；旧记录保留，重复点击继续无法解决版本差异。'),
    (r'Artifact path or content changed|Cached artifact changed|checkpoint changed',
     '任务产物或检查点与保存记录不一致，已停止以保护数据。请查看完整性日志，勿反复重试。'),
    (r'Registered source changed|Direct source frame changed', '原始素材或分析画面已变化，请重新添加素材并新建任务。'),
    (r'daily_allowance_exhausted|daily.*quota|daily.*allowance', '访问码今日额度已用完，请等待额度重置或联系管理员调整每日额度。'),
    (r'insufficient_balance|insufficient_quota|balance.*(?:low|exhaust)', '模型服务余额或可用额度不足，请联系管理员补充后继续。'),
    (r'access_denied|authentication|\b401\b|invalid.*(?:token|api.key)', '访问码或模型服务授权校验失败，请在设置中检查访问码；仍失败时联系管理员检查网关授权。'),
    (r'\b403\b|forbidden', '服务拒绝访问，请检查访问权限及网关设置。'),
    (r'\b429\b|rate.limit|too many requests', '服务请求过于频繁，请稍后继续；若持续出现，请检查服务额度。'),
    (r'expired.*(?:token|grant)|(?:token|grant).*expired', '访问码已过期，请联系管理员续期并在设置中更新。'),
    (r'timeout|timed out|connect_or_response_headers|response_headers', '连接模型服务或等待响应超时，检查点已保留。请检查网络和网关连接后继续。'),
    (r'name resolution|\bdns\b|getaddrinfo', '无法解析服务地址，请检查网络、代理和网关地址。'),
    (r'certificate verify|certificate.*invalid', '服务证书校验失败，请检查系统时间、证书及代理设置。'),
    (r'connection.*(?:reset|refused|closed)|remote.*closed|network.*unreachable', '服务连接中断或无法建立连接，请恢复网络后从检查点继续。'),
    (r'Model call or reported token budget reached', '本次执行已达到模型用量上限，检查点已保存。确认剩余额度后可继续；继续执行会产生新的模型用量。'),
    (r'Each retained speech clip must be', '规划中的口播片段时长不符合要求，需要重新调整计划。'),
    (r'Planner exhausted revision budget', '模型多次修订后仍未生成符合要求的剪辑计划，已保留检查点。'),
    (r'No space left|disk.*full|\bWinError 112\b', '磁盘空间不足，请清理输出盘空间后重试。'),
    (r'Permission denied|Access is denied|\bWinError 5\b', '没有文件读写权限，或文件被其他程序占用，请检查目录权限后重试。'),
    (r'No such file|FileNotFoundError|cannot find the file|\bWinError 2\b|missing source', '所需素材、工具或资源文件不存在，请检查文件路径和软件依赖。'),
    (r'Unknown encoder|Encoder .* not found', '当前渲染工具缺少所需编码器，请修复内置渲染依赖。'),
    (r'Invalid data found|Error while decoding', '媒体数据无法正常解码，请检查素材是否损坏。'),
    (r'already being written', '已有工作台正在使用此工作目录，请退出重复启动的工作台后重试。'),
    (r'Unsupported.*(?:effect|transition)|(?:effect|transition).*unsupported', '这项特效或转场暂不支持外部渲染，请调整要求；原任务已保留。'),
)

STAGES = {'COMPILING': '编译时间线', 'MODEL_TRANSPORT': '连接模型服务',
          'WAITING_FOR_INPUT': '需要处理', 'BLOCKED_RESOURCES': '待补资源',
          'FAILED': '执行失败', 'COMPLETED': '已完成', 'CREATED': '排队中'}


def chinese_diagnostic(value):
    text = str(value or '').strip()
    if not text:
        return ''
    if text in STAGES:
        return STAGES[text]
    for pattern, message in RULES:
        if re.search(pattern, text, re.I):
            # An uncertain submitted request must not invite a blind re-submit.
            if re.search(r'outcome.*(?:uncertain|unknown)|submitted.*unknown', text, re.I):
                message += '请求可能已提交，请恢复连接后查询原请求，不要重复创建任务。'
            if '隐藏' in text:
                message += '（访问凭证已隐藏）'
            return message
    text = re.sub(r'\bcharacters?\b', '字符', text, flags=re.I)
    text = re.sub(r'\bwords?\b', '词', text, flags=re.I)
    if re.search(r'[\u3400-\u9fff]', text):
        # Preserve existing Chinese explanations and technical identifiers,
        # paths, times and quantities, but do not leak raw English sentences.
        text = re.sub(r'\b[A-Za-z][\w-]*(?:[ \t]+[A-Za-z][\w-]*){2,}',
                      '（技术详情见任务日志）', text)
        return text
    reference = hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]
    return f'任务遇到暂未分类的技术错误，请查看任务日志或向管理员提供诊断编号：{reference}。'
