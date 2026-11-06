"""Fixed offline protocol evaluation. This does not measure model answer quality."""
from pathlib import Path
import argparse
import json
import subprocess
import sys
import tempfile
from time import perf_counter
import xml.etree.ElementTree as ET

CASES = [
    'tests/test_agent.py::test_multiround_agent_inspects_compares_and_finishes',
    'tests/test_agent.py::test_invalid_tool_or_arguments_fail_closed',
    'tests/test_agent.py::test_model_round_limit_is_enforced',
    'tests/test_agent.py::test_cancellation_stops_agent_model_and_tool_phases',
    'tests/test_agent.py::test_transport_failure_is_terminal_and_does_not_leak_provider_body',
    'tests/test_agent.py::test_final_without_evidence_is_not_accepted',
    'tests/test_v02.py::test_no_progress_guard_and_trace_observability',
    'tests/test_v02.py::test_queue_cancellation_does_not_start_queued_job_and_failed_job_isolated',
]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('artifacts/runtime-eval.json'))
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    started=perf_counter()
    with tempfile.TemporaryDirectory(prefix='report-runtime-eval-') as directory:
        junit=Path(directory)/'results.xml'
        result=subprocess.run([sys.executable,'-m','pytest','-q',*CASES,'--junitxml='+str(junit)],cwd=root,check=False)
        cases=[]
        if junit.exists():
            for case in ET.parse(junit).iter('testcase'):
                status='failed' if case.find('failure') is not None or case.find('error') is not None else 'skipped' if case.find('skipped') is not None else 'passed'
                cases.append({'name':case.attrib['name'],'status':status,'seconds':float(case.attrib.get('time',0))})
    report={'kind':'runtime_protocol_evaluation','external_model_called':False,
            'scope':'Fixed MockTransport and local function fixtures; tests runtime behavior, not model quality.',
            'status':'passed' if result.returncode==0 and cases else 'failed',
            'elapsed_seconds':round(perf_counter()-started,3),'total':len(cases),
            'passed':sum(case['status']=='passed' for case in cases),'cases':cases}
    output=args.output if args.output.is_absolute() else root/args.output
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f"{report['status'].upper()}: {report['passed']}/{report['total']} runtime cases; {output}")
    raise SystemExit(0 if report['status']=='passed' else 1)


if __name__=='__main__':main()
