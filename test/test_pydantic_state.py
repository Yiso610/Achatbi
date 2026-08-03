"""
Test script to validate the Pydantic AgentState implementation.

This script tests:
1. State initialization with required fields only
2. Default values for optional fields
3. Field validation constraints
4. Helper methods
5. Pydantic error handling
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.state import AgentState


TEST_DATABASE_PATH = "data/uploads/test-source.db"


def test_state_initialization():
    """Test that state can be initialized with all required fields."""
    print("[TEST] State Initialization...")
    try:
        state = AgentState(
            question="What are the top 5 artists?",
            database_path=TEST_DATABASE_PATH,
        )
        
        # Verify required field
        assert state.question == "What are the top 5 artists?"
        assert state.database_path == TEST_DATABASE_PATH
        
        # Verify default values
        assert state.is_relevant == False
        assert state.sql_query == ""
        assert state.reasoning == ""
        assert state.validation_passed == False
        assert state.validation_error == ""
        assert state.query_result == []
        assert state.error == ""
        assert state.retry_count == 0
        assert state.visualization_spec is None
        assert state.final_response == ""
        
        print("   [PASS] State initialized with correct defaults")
        return True
    except Exception as e:
        print(f"   [FAIL] State initialization failed: {e}")
        return False


def test_field_validation():
    """Test field validation constraints."""
    print("\n[TEST] Field Validation...")
    try:
        # Test retry_count validation (must be >= 0)
        state = AgentState(
            question="Test question",
            database_path=TEST_DATABASE_PATH,
        )

        # A request without a selected database must be rejected.
        try:
            AgentState(question="Test question")
            print("   [FAIL] Missing database_path should raise error")
            return False
        except ValueError:
            print("   [PASS] Missing database_path rejected")
        
        # Valid retry count
        state.retry_count = 5
        assert state.retry_count == 5
        print("   [PASS] Valid retry_count accepted")
        
        # Invalid retry count (negative)
        try:
            state.retry_count = -1
            print("   [FAIL] Negative retry_count should raise error")
            return False
        except ValueError as e:
            print(f"   [PASS] Negative retry_count rejected: {e}")
        
        # Test query_result validation (must be list)
        state.query_result = [{"name": "Artist 1"}]
        assert len(state.query_result) == 1
        print("   [PASS] Valid query_result accepted")
        
        return True
    except Exception as e:
        print(f"   [FAIL] Field validation test failed: {e}")
        return False


def test_helper_methods():
    """Test helper methods."""
    print("\n[TEST] Helper Methods...")
    try:
        state = AgentState(
            question="Test question",
            database_path=TEST_DATABASE_PATH,
        )
        
        # Test has_error()
        assert state.has_error() == False
        state.error = "Some error"
        assert state.has_error() == True
        print("   [PASS] has_error() works correctly")
        
        # Test is_complete()
        state2 = AgentState(
            question="Test",
            database_path=TEST_DATABASE_PATH,
        )
        assert state2.is_complete() == False
        state2.final_response = "Here is your answer"
        assert state2.is_complete() == True
        print("   [PASS] is_complete() works correctly")
        
        # Test get_error_message()
        state3 = AgentState(
            question="Test",
            database_path=TEST_DATABASE_PATH,
        )
        state3.validation_error = "Validation failed"
        assert state3.get_error_message() == "Validation failed"
        state3.error = "Execution failed"
        assert state3.get_error_message() == "Validation failed"  # validation_error takes precedence
        print("   [PASS] get_error_message() works correctly")
        
        # Test reset_errors()
        state3.reset_errors()
        assert state3.error == ""
        assert state3.validation_error == ""
        print("   [PASS] reset_errors() works correctly")
        
        return True
    except Exception as e:
        print(f"   [FAIL] Helper methods test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_model_dump():
    """Test conversion to dict for LangGraph compatibility."""
    print("\n[TEST] Model Dump (Dict Conversion)...")
    try:
        state = AgentState(
            question="Test question",
            database_path=TEST_DATABASE_PATH,
        )
        state_dict = state.model_dump()
        
        # Verify it's a dict
        assert isinstance(state_dict, dict)
        
        # Verify all fields are present
        assert "question" in state_dict
        assert "database_path" in state_dict
        assert "is_relevant" in state_dict
        assert "sql_query" in state_dict
        assert "retry_count" in state_dict
        
        # Verify values
        assert state_dict["question"] == "Test question"
        assert state_dict["is_relevant"] == False
        assert state_dict["retry_count"] == 0
        
        print("   [PASS] model_dump() produces correct dict")
        
        # Test reconstruction from dict
        state2 = AgentState(**state_dict)
        assert state2.question == state.question
        assert state2.retry_count == state.retry_count
        print("   [PASS] State can be reconstructed from dict")
        
        return True
    except Exception as e:
        print(f"   [FAIL] Model dump test failed: {e}")
        return False


def test_field_updates():
    """Test updating state fields."""
    print("\n[TEST] Field Updates...")
    try:
        state = AgentState(
            question="Test question",
            database_path=TEST_DATABASE_PATH,
        )
        
        # Update various fields
        state.is_relevant = True
        state.sql_query = "SELECT * FROM Artist LIMIT 5"
        state.reasoning = "Get top 5 artists"
        state.query_result = [{"name": "Artist 1"}, {"name": "Artist 2"}]
        state.retry_count = 2
        
        # Verify updates
        assert state.is_relevant == True
        assert state.sql_query == "SELECT * FROM Artist LIMIT 5"
        assert state.reasoning == "Get top 5 artists"
        assert len(state.query_result) == 2
        assert state.retry_count == 2
        
        print("   [PASS] All fields can be updated correctly")
        return True
    except Exception as e:
        print(f"   [FAIL] Field update test failed: {e}")
        return False


def main():
    """Run all tests."""
    print("=" * 60)
    print("Pydantic AgentState - Validation Test Suite")
    print("=" * 60)
    print()
    
    tests = [
        ("State Initialization", test_state_initialization),
        ("Field Validation", test_field_validation),
        ("Helper Methods", test_helper_methods),
        ("Model Dump", test_model_dump),
        ("Field Updates", test_field_updates),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            passed = test_func()
            results.append((name, passed))
        except Exception as e:
            print(f"\n[ERROR] {name} test crashed: {e}")
            import traceback
            traceback.print_exc()
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
        print("\n[SUCCESS] All Pydantic state tests passed!")
        return 0
    else:
        print("\n[WARNING] Some tests failed. Please review above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
