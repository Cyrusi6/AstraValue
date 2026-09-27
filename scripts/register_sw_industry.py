"""Register a verified SW alternative classification for a frozen research task."""
import argparse
from analysis.research.workspace import ResearchWorkspace
from analysis.research.sw_industry import register

ap=argparse.ArgumentParser();ap.add_argument('--research-id',required=True);ap.add_argument('--result',required=True)
a=ap.parse_args();print(register(ResearchWorkspace(),a.research_id,a.result))
