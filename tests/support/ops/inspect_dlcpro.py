#!/usr/bin/env python3
"""Read-only DLC inventory. Never starts an owner or writes a parameter."""
import argparse
import datetime
import json
import re
from pathlib import Path

from ldcon.drivers.dlcpro import Console, DLCPro


class ReadOnlyConsole:
    def __init__(self, console):
        self.console = console
        self.commands = []

    def command(self, expression, *, writing=False):
        if writing or not re.fullmatch(r"\(param-ref '[a-z][a-z0-9:-]*\)", expression):
            raise PermissionError('Inventory only permits scalar parameter reads')
        self.commands.append(expression)
        return self.console.command(expression)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',required=True)
    parser.add_argument('--serial',required=True)
    parser.add_argument('--firmware',required=True)
    parser.add_argument('--channel',type=int,default=1)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error('Output exists; preserve the previous inventory')
    console=Console.connect(args.host,timeout_s=3.)
    guard=ReadOnlyConsole(console);driver=DLCPro(guard)
    try:
        driver.verify_identity(args.serial,args.firmware)
        record=dict(observed_at=datetime.datetime.now().astimezone().isoformat(),
            hardware_writes=False,service_operations=False,identity=driver.identity(args.channel),
            snapshot=driver.snapshot(args.channel),
            scan={key:driver.read(f'laser{args.channel}:scan:{key}') for key in
                  ('enabled','hold','signal-type','frequency','output-channel','unit','amplitude','offset')})
        record['read_commands']=guard.commands
    finally:console.close()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as stream:json.dump(record,stream,indent=2);stream.write('\n')
    print(json.dumps(record,indent=2))


if __name__=='__main__':main()
