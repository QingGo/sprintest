#!/bin/bash
set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== Sprintest Performance Benchmark ===${NC}"
echo "Platform: $(uname -m) $(uname -s)"
echo "Python: $(uv run python --version)"

# 1. Cleanup
echo -e "\n${BLUE}1. Cleaning up environment...${NC}"
pkill -f sprintest.daemon || true
rm -f .sprintest/*.lock .sprintest/*.sock .sprintest/status.json

# 2. Pytest Cold Start
echo -e "\n${BLUE}2. Running Pytest (Cold Start - Heavy Imports)...${NC}"
start_time=$(date +%s.%N)
uv run pytest -s examples/test_ai_model.py
end_time=$(date +%s.%N)
cold_duration=$(echo "scale=3; $end_time - $start_time" | bc)
echo -e "${GREEN}Cold Start Duration: ${cold_duration}s${NC}"

# 3. Sprintest First Run (Cold Start)
echo -e "\n${BLUE}3. Running Sprintest (First Run - Daemon Startup & Loading)...${NC}"
start_time=$(date +%s.%N)
uv run stest examples/test_ai_model.py
end_time=$(date +%s.%N)
first_run_duration=$(echo "scale=3; $end_time - $start_time" | bc)
echo -e "${GREEN}First Run Duration: ${first_run_duration}s${NC}"

# 4. Sprintest Warm Start
echo -e "\n${BLUE}4. Running Sprintest (Warm Start - Hot Reload)...${NC}"
start_time=$(date +%s.%N)
uv run stest examples/test_ai_model.py
end_time=$(date +%s.%N)
warm_duration=$(echo "scale=3; $end_time - $start_time" | bc)
echo -e "${GREEN}Warm Start Duration: ${warm_duration}s${NC}"

# 5. Summary
speedup=$(echo "scale=3; $cold_duration / $warm_duration" | bc)
echo -e "\n${BLUE}=== Summary ===${NC}"
echo "Pytest (Cold): ${cold_duration}s"
echo "Sprintest (First Run): ${first_run_duration}s"
echo "Sprintest (Warm): ${warm_duration}s"
echo -e "${GREEN}Total Speedup (Cold vs Warm): ${speedup}x${NC}"

# Cleanup daemon
pkill -f sprintest.daemon || true
