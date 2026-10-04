"""Bounded DeepSeek evaluation of approved fictional interactions; no tools execute."""
import argparse
import asyncio
import difflib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(os.environ.get('ATHENA_PROJECT_ROOT', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT/'src'))
from athena.config import load_local_environment
from athena.prompts import interaction_examples, interaction_style, read_voice_prompt

PATTERNS = {
 'weather':[r'28|twenty.?eight', r'rain'], 'tomorrow':[r'warm',r'rain'],
 'greeting':[r'morning'], 'wait':[r'time|wait|ready|no rush|standing by'],
 'clarify':[r'adele',r'alan',r'\?'], 'quiet':[r'very well|understood|quiet|standby|wake|alright|okay'],
 'assignments':[r'physics',r'history',r'tomorrow'], 'priority':[r'physics',r'eight|8'],
 'denial':[r'denied|blocked|can.t|cannot',r'channel|post',r'assignment'],
 'deadline':[r'physics',r'tonight',r'nine|9'],
 'saved':[r'noted|remember|saved|favour|favor',r'evening|dinner'],
 'recall':[r'dinner',r'no |haven.t|not |never|specific'],
 'memory_failure':[r'couldn.t|can.t|failed|unable|not saved',r'conversation|chat|permanent'],
 'estimate':[r'possibly|might|unlikely|wouldn.t|tight|ambitious|probably|possible',r'physics'],
 'search_pending':[r'check|search|look|report'], 'news':[r'metro',r'typhoon|drill'],
 'irrelevant':[r'not|aren.t|irrelevant|don.t',r'search|narrow|refine|news'],
 'clock':[r'7[:.]42|seven.?forty.?two'], 'started':[r'downloading|started|download.*progress|download.*underway'],
 'progress':[r'60|sixty',r'2[.-]9|two.?point.?nine',r'1[.-]2|one.?point.?two'],
 'download_failure':[r'timeout|timed out',r'stopped|failed|incomplete|not.*complet|hasn.t',r'retry|again'],
 'file':[r'created|saved',r'checked|verified|contains|conversation|chat'],
 'sandbox':[r'separate|isolated|workspace',r'untouched|approv|won.t.*touch|won.t.*change'],
 'tests_failed':[r'not|isn.t',r'duplicate',r'fix|correct'], 'alarm':[r'ten|10',r'alarm|set|sound'],
 'music':[r'paused',r'calm|quiet',r'volume|lower'],
 'honesty':[r'right|correct|mistake|unverified|without|confirmation',r'check|verify|actual'],
}


def assess(case, reply):
    failures=[]
    for check in case.get('checks', []):
        for pattern in PATTERNS[check]:
            if not re.search(pattern, reply, re.I): failures.append(check+':'+pattern)
    if len(reply.split())>65: failures.append('too verbose')
    if case['id'] not in {1,4} and re.search(r'\bsir\b',reply,re.I): failures.append('unnecessary honorific')
    if case['id']==26 and re.search(r'can.t run|cannot run|unable to run',reply,re.I): failures.append('invented limitation')
    if re.search(r'https?://|\*\*|<think>|<analysis>|^\s*on it\b',reply,re.I): failures.append('filler/format')
    if case['id'] in {11,15,19,24,27,30} and re.search(
        r'completed successfully|successfully downloaded|saved permanently|all tests pass',reply,re.I):
        failures.append('unsupported success')
    return failures


async def run(args):
    load_local_environment()
    from openai import AsyncOpenAI
    semaphore=asyncio.Semaphore(2)
    prompt=read_voice_prompt()+'\n\n'+interaction_style()
    cases=interaction_examples()
    selected={int(value) for value in args.ids.split(',')} if args.ids else None
    report=[]
    async with AsyncOpenAI(api_key=os.environ['DEEPSEEK_API_KEY'],base_url='https://api.deepseek.com',
                           timeout=25,max_retries=0) as client:
        async def evaluate(case):
            if case['mode']=='engine':
                report.append({'id':case['id'],'mode':'engine','status':'reference-only',
                    'note':'Activation/approval behaviour requires coordinator tests, not a model completion.'})
                return
            async with semaphore:
                try:
                    response=await client.chat.completions.create(
                        model=os.environ.get('DEEPSEEK_MODEL','deepseek-v4-flash'),temperature=0,max_tokens=110,
                        extra_body={'thinking':{'type':'disabled'}},messages=[
                            {'role':'system','content':prompt},
                            {'role':'system','content':'Isolated fictional evaluation, NOT a live user task. '
                             'Produce only the assistant spoken response using these stipulated verified facts. '
                             'Do not execute any actions. Context: '+case['context']},
                            {'role':'user','content':case['user']}])
                    reply=response.choices[0].message.content or ''
                    failures=assess(case,reply)
                    report.append({'id':case['id'],'reply':reply,'expected':case['expected'],
                        'pass':not failures,'failures':failures,
                        'lexical_similarity':round(difflib.SequenceMatcher(None,
                            case['expected'].casefold(),reply.casefold()).ratio(),3),
                        'usage':response.usage.model_dump() if response.usage else None})
                    print(f"Case {case['id']}: {'PASS' if not failures else 'REVIEW'}",flush=True)
                except Exception as error:
                    report.append({'id':case['id'],'pass':False,'error':str(error)})
                    print(f"Case {case['id']}: ERROR",flush=True)
        await asyncio.gather(*(evaluate(case) for case in cases if not selected or case['id'] in selected))
    report.sort(key=lambda item:item['id'])
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ids',help='Comma-separated failed case IDs to retest, avoiding a full billed rerun')
    parser.add_argument('--output',type=Path,default=Path('outputs/interactions/report.json'))
    asyncio.run(run(parser.parse_args()))
