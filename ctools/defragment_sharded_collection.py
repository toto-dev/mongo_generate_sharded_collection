#!/usr/bin/env python3
#

import argparse
import asyncio
import motor.motor_asyncio
import pymongo
import sys

from common import Cluster

# Ensure that the caller is using python 3
if (sys.version_info[0] < 3):
    raise Exception("Must be using Python 3")


async def main(args):
    cluster = Cluster(args.uri)
    if not (await cluster.adminDb.command('ismaster'))['msg'] == 'isdbgrid':
        raise Exception("Not connected to mongos")

    if args.dryrun is not None:
        print(f'Performing a dry run ...')

    # Phase 0: Fetch all chunks in memory and bring all shards to be at the collection version in
    # order to allow concurrent merges across shards without causing refresh stalls
    num_chunks = await cluster.configDb.chunks.count_documents({'ns': args.ns})
    global num_chunks_processed
    num_chunks_processed = 0
    shardToChunks = {}
    collectionVersion = None
    async for c in cluster.configDb.chunks.find({'ns': args.ns}, sort=[('min', pymongo.ASCENDING)]):
        shardId = c['shard']
        if collectionVersion is None:
            collectionVersion = c['lastmod']
        if c['lastmod'] > collectionVersion:
            collectionVersion = c['lastmod']
        if shardId not in shardToChunks:
            shardToChunks[shardId] = {'chunks': []}
        shard = shardToChunks[shardId]
        shard['chunks'].append(c)
        num_chunks_processed += 1
        print(
            f'Phase 0: {round((num_chunks_processed * 100)/num_chunks, 1)}% ({num_chunks_processed} chunks) fetched',
            end='\r')
    print(f'Collection version: {collectionVersion}')

    # Phase 1: Bring all shards to be at the same major chunk version
    sem = asyncio.Semaphore(2)

    async def merge_chunks_on_shard(shard):
        shardChunks = shardToChunks[shard]['chunks']
        if len(shardChunks) < 2:
            return

        consecutiveChunks = []
        for c in shardChunks:
            if len(consecutiveChunks) == 0 or consecutiveChunks[-1]['max'] != c['min']:
                consecutiveChunks = [c]
                continue
            else:
                consecutiveChunks.append(c)

            estimated_size_of_run_mb = len(consecutiveChunks) * 48
            if estimated_size_of_run_mb > 240:
                mergeCommand = {
                    'mergeChunks': args.ns,
                    'bounds': [consecutiveChunks[0]['min'], consecutiveChunks[-1]['max']]
                }

                async with sem:
                    if args.dryrun:
                        print(f"Merging on {shard}: {mergeCommand}")
                    else:
                        await cluster.adminDb.command(mergeCommand)
                    consecutiveChunks = []

    tasks = []
    for s in shardToChunks:
        maxShardVersionChunk = max(shardToChunks[s]['chunks'], key=lambda c: c['lastmod'])
        shardVersion = maxShardVersionChunk['lastmod']
        print(f"{s}: {maxShardVersionChunk['lastmod']}: ", end='')
        if shardVersion.time == collectionVersion.time:
            print(' Skipping due to matching shard version ...')
        else:
            print(' Bumping ...')
            tasks.append(asyncio.ensure_future(merge_chunks_on_shard(s)))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    argsParser = argparse.ArgumentParser(description='Tool to defragment a sharded cluster')
    argsParser.add_argument(
        'uri', help='URI of the mongos to connect to in the mongodb://[user:password@]host format',
        metavar='uri', type=str, nargs=1)
    argsParser.add_argument('--dryrun', help='Whether to perform a dry run or actual merges',
                            action='store_true')
    argsParser.add_argument('--ns', help='The namespace to defragment', metavar='ns', type=str,
                            required=True)

    args = argsParser.parse_args()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main(args))