"""Explicit live visual-to-language diagnostic, with a durable bounded report."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .credential_store import credential_environment
from .model_router import create_provider
from .provider import ProviderError
from .deepseek import ModelOutputError
from .task_store import atomic_json, digest, stamp


def check_models(config, report_path, *, frames=None, time_domain='source', progress=None):
    report_path = Path(report_path)
    if not config.get('vision_provider'):
        raise ValueError('This check requires the multimodel profile')
    provider = create_provider(config)
    report = {'schema': 'jianying-multimodel-check-v2', 'timestamp': stamp(),
              'status': 'running', 'codex_runtime_required': False,
              'full_edit_run': False, 'continuous_video_reviewed': False}
    def save():
        report['usage'] = provider.usage
        atomic_json(report_path, report)
    def phase(name):
        report['stage'] = name
        save()
        if progress:
            progress(name)
    save()
    try:
        with TemporaryDirectory(prefix='jianying-model-check-') as folder:
            fixture = frames is None
            if fixture:
                from PIL import Image, ImageDraw
                path = Path(folder)/'color-fixture.png'
                image = Image.new('RGB', (240, 160), 'white')
                draw = ImageDraw.Draw(image)
                draw.rectangle((10, 10, 100, 150), fill='red')
                draw.ellipse((130, 30, 225, 125), fill='blue')
                image.save(path)
                frames = [{'path': str(path), 'sha256': digest(path), 'requested_us': 0,
                           'asset_id': 'synthetic-color-fixture'}]
            if not 1 <= len(frames) <= 8:
                raise ValueError('Diagnostic accepts 1..8 verified frames')
            phase('source_analysis' if time_domain == 'source' else 'omni_output_analysis')
            observed = provider.observe_frames(frames, focus='分析画面中的可见主体、结构、文字与构图',
                                           time_domain=time_domain)
            report['visual_analysis'] = observed
            if fixture and time_domain == 'source' and config.get('complex_vision_provider'):
                phase('qwen_max_complex_source_route_probe')
                report['complex_source_analysis'] = provider.observe_frames(frames,
                    focus='显式路由探针：分析图形结构，不把测试图形描述为实际复杂长视频。',
                    context={'forced_routing_probe': True}, time_domain='source', complex_scene=True)
            if fixture or time_domain == 'output':
                phase('qwen_omni_output_review')
                report['output_review'] = provider.observe_frames(frames,
                    focus='成片画面质检：检查实际图片可见的问题，不推断未提供的声音和动作。',
                    context={'synthetic_fixture':fixture}, time_domain='output', review=True)
                if report['output_review']['provider'] != 'qwen_omni':
                    raise ModelOutputError('Output review was routed away from Omni')
            evidence = {key: report[key] for key in ('visual_analysis', 'complex_source_analysis', 'output_review') if key in report}
            phase('deepseek_text_from_visual_evidence')
            with credential_environment(config['provider']):
                if fixture:
                    report['language_result'] = provider._json(
                        '根据视觉证据返回JSON {"left_color":"英文颜色","right_color":"英文颜色"}，不猜测。',
                        evidence)
                    result = report['language_result']
                    if result.get('left_color') != 'red' or result.get('right_color') != 'blue':
                        raise ModelOutputError('Visual-to-language color fixture did not match')
                else:
                    report['language_result'] = provider._json(
                        '你是中文电商剪辑师，仅根据所给Qwen视觉证据，返回JSON '
                        '{"visible_product":"主体","new_copy":"一句全新客观口播",'
                        '"edit_suggestion":"画面编排建议","evidence_ids":["引用的evidence_id"],'
                        '"limitations":"不能确认的内容"}。不要虚构价格、功效或声音。',
                        evidence)
                    result = report['language_result']
                    if any(not isinstance(result.get(k), str) or not result[k].strip()
                           for k in ('visible_product', 'new_copy', 'edit_suggestion', 'limitations')):
                        raise ModelOutputError('Language diagnostic returned incomplete fields')
                    allowed = {item['evidence_id'] for item in evidence.values()}
                    refs = result.get('evidence_ids')
                    if not isinstance(refs, list) or not refs or any(r not in allowed for r in refs):
                        raise ModelOutputError('Language diagnostic did not cite visual evidence')
            report.update(status='passed', stage='completed',
                          deepseek_received_raw_media=False, tested_input='synthetic_fixture' if fixture else 'native_preview_frames')
    except (ProviderError, ModelOutputError, ValueError, OSError) as exc:
        report.update(status='error', message=str(exc), error_type=type(exc).__name__)
    finally:
        save()
    return report


def main():
    import argparse
    from .runtime import load_config
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(Path(__file__).resolve().parents[1]/'config.native.json'))
    parser.add_argument('--out', required=True)
    parser.add_argument('--frame-manifest')
    parser.add_argument('--time-domain', choices=('source', 'output'), default='source')
    args = parser.parse_args()
    frames = None
    if args.frame_manifest:
        all_frames = json.loads(Path(args.frame_manifest).read_text(encoding='utf-8-sig'))['frames']
        frames = [all_frames[i] for i in sorted({0, len(all_frames)//2, len(all_frames)-1})]
    report = check_models(load_config(args.config), args.out, frames=frames, time_domain=args.time_domain,
                          progress=lambda name: print(json.dumps({'stage': name}), flush=True))
    print(json.dumps({'status': report['status'], 'report': str(Path(args.out).resolve()),
                      'message': report.get('message'), 'providers': report['usage'].get('providers'),
                      'language_result': report.get('language_result')}, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
