# reset_data.py

from core.local_data import clear_local_data

def main():
    result = clear_local_data(include_downloaded=True)
    print(f"Datos reiniciados. Elementos eliminados: {result['removed_entries']}")


if __name__ == "__main__":
    main()
