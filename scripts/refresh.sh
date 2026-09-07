cd "/Users/wendell/Desktop/tiktok_crawler"
source "/Users/wendell/miniconda3/etc/profile.d/conda.sh"
conda activate douyin-crawler

PYTHONPATH="/Users/wendell/Desktop/tiktok_crawler/src"
export PYTHONPATH

python -m dycrawler refresh \
  --database "/Users/wendell/Desktop/tiktok_crawler/data/douyin.db" \
  --profile "/Users/wendell/Desktop/tiktok_crawler/data/browser_profile" \
  --limit 20
