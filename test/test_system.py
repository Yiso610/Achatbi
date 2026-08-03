"""
Test script for validating the Text-to-SQL system.

This script tests:
1. Configuration loading
2. Database connectivity
3. LLM initialization
4. Basic agent workflow
5. Error handling

Run this before deploying to ensure everything works.
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def get_test_database_path() -> Path:
    """Use a database source added through the same flow as the application."""
    from infrastructure.data_sources import list_database_sources

    sources = list_database_sources()
    if not sources:
        raise RuntimeError(
            "No database source is available. Add one from the data catalog first."
        )
    return sources[0]


def test_configuration():
    """Test configuration loading."""
    print("[TEST] Configuration...")
    try:
        from infrastructure.config import get_config
        config = get_config()
        print(f"   [PASS] Config loaded successfully")
        for role in ("router", "sql_generator", "reflector", "visualizer"):
            chain = " -> ".join(
                f"{target.provider}:{target.model}"
                for target in config.get_model_chain(role)
            )
            print(f"   - {role}: {chain}")
        print(f"   - Max Retries: {config.max_retry_count}")
        return True
    except Exception as e:
        print(f"   [FAIL] Configuration failed: {e}")
        return False


def test_database():
    """Test database connectivity."""
    print("\n[TEST] Database...")
    try:
        from infrastructure.db_manager import DatabaseManager
        database_path = get_test_database_path()
        db = DatabaseManager(database_path)
        
        # Test schema retrieval
        schema = db.get_annotated_schema()
        print(f"   [PASS] Database connected")
        print(f"   - Source: {database_path.name}")
        
        # Test table listing
        tables = db.get_table_names()
        print(f"   - Found {len(tables)} tables: {', '.join(tables[:5])}...")
        
        # Test simple query
        if not tables:
            print("   [FAIL] No business tables found")
            return False

        table = tables[0]
        results, error = db.execute_query(
            f'SELECT COUNT(*) as count FROM "{table}"'
        )
        if error:
            print(f"   [FAIL] Query failed: {error}")
            return False
        else:
            count = results[0]['count']
            print(f"   - {table} row count: {count}")
        
        return True
    except Exception as e:
        print(f"   [FAIL] Database test failed: {e}")
        return False


def test_llm():
    """Test LLM initialization."""
    print("\n[TEST] LLM Initialization...")
    try:
        from infrastructure.llm import get_router_llm, get_sql_generator_llm
        
        # Test router LLM
        router_llm = get_router_llm()
        print(f"   [PASS] Router LLM initialized")
        
        # Test SQL generator LLM
        sql_llm = get_sql_generator_llm()
        print(f"   [PASS] SQL Generator LLM initialized")
        
        return True
    except Exception as e:
        print(f"   [FAIL] LLM initialization failed: {e}")
        print(
            "   [INFO] Check the provider keys and *_MODEL_CHAIN "
            "settings in your .env file"
        )
        return False


def test_basic_workflow():
    """Test basic agent workflow."""
    print("\n[TEST] Basic Workflow...")
    try:
        from agents.graph import run_agent
        
        # Test with a simple question
        question = "按品牌统计测点数量，并按数量从高到低排列"
        print(f"   Question: '{question}'")
        
        result = run_agent(question, get_test_database_path())
        
        if result.is_relevant:
            print(f"   [PASS] Intent routing: RELEVANT")
        else:
            print(f"   [FAIL] Intent routing failed")
            return False
        
        if result.sql_query:
            print(f"   [PASS] SQL generated")
            print(f"   - Query: {result.sql_query[:50]}...")
        else:
            print(f"   [FAIL] SQL generation failed")
            return False
        
        if result.query_result:
            print(f"   [PASS] Query executed successfully")
            print(f"   - Results: {len(result.query_result)} rows")
        else:
            print(f"   [WARNING] Query returned no results")
        
        return True
    except Exception as e:
        print(f"   [FAIL] Workflow test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_error_handling():
    """Test error handling with irrelevant question."""
    print("\n[TEST] Error Handling...")
    try:
        from agents.graph import run_agent
        
        # Test with irrelevant question
        question = "What is the capital of France?"
        print(f"   Question: '{question}'")
        
        result = run_agent(question, get_test_database_path())
        
        if not result.is_relevant:
            print(f"   [PASS] Correctly identified as irrelevant")
            print(f"   - Response: {result.final_response[:50]}...")
            return True
        else:
            print(f"   [FAIL] Should have been marked irrelevant")
            return False
    except Exception as e:
        print(f"   [FAIL] Error handling test failed: {e}")
        return False


def main():
    """Run all tests."""
    print("=" * 60)
    print("Text-to-SQL System - Test Suite")
    print("=" * 60)
    print()
    
    tests = [
        ("Configuration", test_configuration),
        ("Database", test_database),
        ("LLM", test_llm),
        ("Basic Workflow", test_basic_workflow),
        ("Error Handling", test_error_handling),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            passed = test_func()
            results.append((name, passed))
        except Exception as e:
            print(f"\n[ERROR] {name} test crashed: {e}")
            results.append((name, False))
    
    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    
    for name, passed in results:
        status = "[PASS]" if passed else "[FAIL]"
        print(f"{status} - {name}")
    
    total = len(results)
    passed_count = sum(1 for _, p in results if p)
    
    print()
    print(f"Total: {passed_count}/{total} tests passed")
    
    if passed_count == total:
        print("\n[SUCCESS] All tests passed! System is ready to use.")
        return 0
    else:
        print("\n[WARNING] Some tests failed. Please fix the issues above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
