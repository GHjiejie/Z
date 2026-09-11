import argparse

parser = argparse.ArgumentParser(description="Subgraph Interrupt Demo")
parser.add_argument('--city', type=str, default='New York', help='City to travel to')  
parser.add_argument('--budget', type=int, default=1000, help='Budget for the trip')

args = parser.parse_args()

print(f"City: {args.city}")
print(f"Budget: {args.budget}")