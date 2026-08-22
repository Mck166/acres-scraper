#!/usr/bin/env python3
"""Test script for geocoding addresses in Nova Scotia"""

import time
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError, GeocoderUnavailable
from geopy.adapters import RequestsAdapter

def test_geocoding_method_1():
    """Test method 1: Basic geocoding with SSL verification disabled"""
    print("\n" + "="*60)
    print("TEST METHOD 1: Basic geocoding with SSL disabled")
    print("="*60)
    
    address = "259 Old Courthouse Branch Road, Salmon River"
    query_address = f"{address}, Nova Scotia, Canada"
    
    try:
        import ssl
        import certifi
        
        # Create adapter with proper parameters
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        
        adapter = RequestsAdapter(
            proxies=None,
            ssl_context=ssl_context
        )
        
        geolocator = Nominatim(
            user_agent="PropertyScraper/1.0 (Nova Scotia Property Data Collection)",
            timeout=30,  # Increased timeout
            adapter_factory=lambda: adapter
        )
        
        print(f"Geocoding: {query_address}")
        location = geolocator.geocode(
            query_address,
            country_codes="ca",
            exactly_one=True,
            timeout=30
        )
        
        if location:
            print(f"✓ SUCCESS!")
            print(f"  Latitude: {location.latitude}")
            print(f"  Longitude: {location.longitude}")
            print(f"  Full Address: {location.address}")
            return location.latitude, location.longitude
        else:
            print("✗ No location found")
            return None
            
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return None

def test_geocoding_method_2():
    """Test method 2: Using requests library directly with longer timeout"""
    print("\n" + "="*60)
    print("TEST METHOD 2: Direct requests to Nominatim API")
    print("="*60)
    
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    address = "259 Old Courthouse Branch Road, Salmon River"
    query_address = f"{address}, Nova Scotia, Canada"
    
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": query_address,
        "format": "json",
        "limit": 1,
        "countrycodes": "ca",
        "addressdetails": 1
    }
    
    headers = {
        "User-Agent": "PropertyScraper/1.0 (Nova Scotia Property Data Collection)"
    }
    
    try:
        print(f"Geocoding: {query_address}")
        # Increased timeout to 30 seconds
        response = requests.get(url, params=params, headers=headers, timeout=30, verify=False)
        print(f"Response status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            if data and len(data) > 0:
                result = data[0]
                lat = float(result.get("lat", 0))
                lon = float(result.get("lon", 0))
                print(f"✓ SUCCESS!")
                print(f"  Latitude: {lat}")
                print(f"  Longitude: {lon}")
                print(f"  Display Name: {result.get('display_name', '')}")
                return lat, lon
            else:
                print("✗ No results in response")
                print(f"Response: {response.text[:200]}")
                return None
        else:
            print(f"✗ HTTP Error {response.status_code}")
            print(f"Response: {response.text[:200]}")
            return None
            
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return None

def test_geocoding_method_4():
    """Test method 4: Try Google Geocoding API (if available) or alternative"""
    print("\n" + "="*60)
    print("TEST METHOD 4: Alternative - Using simpler address")
    print("="*60)
    
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    # Try with just the town name first to test connectivity
    test_addresses = [
        "Salmon River, Nova Scotia, Canada",
    ]
    
    url = "https://nominatim.openstreetmap.org/search"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }
    
    for addr in test_addresses:
        print(f"\nTrying: {addr}")
        params = {
            "q": addr,
            "format": "json",
            "limit": 1,
            "countrycodes": "ca",
        }
        
        try:
            print("  Making request...")
            response = requests.get(url, params=params, headers=headers, timeout=30, verify=False)
            print(f"  Response status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                print(f"  Results returned: {len(data) if data else 0}")
                if data and len(data) > 0:
                    result = data[0]
                    lat = float(result.get("lat", 0))
                    lon = float(result.get("lon", 0))
                    print(f"  ✓ Found: {lat}, {lon}")
                    print(f"  Address: {result.get('display_name', '')[:80]}")
                    return lat, lon
                else:
                    print(f"  ✗ No results")
                    print(f"  Response preview: {response.text[:200]}")
            else:
                print(f"  ✗ HTTP {response.status_code}")
                print(f"  Response: {response.text[:200]}")
            time.sleep(2)  # Rate limiting
        except requests.exceptions.Timeout:
            print(f"  ✗ Request timed out - Nominatim may be slow or blocked")
        except Exception as e:
            print(f"  ✗ Error: {e}")
            time.sleep(2)
    
    return None

def main():
    print("Geocoding Test Script")
    print("Testing address: 259 Old Courthouse Branch Road, Salmon River")
    
    # Test method 1
    result1 = test_geocoding_method_1()
    time.sleep(2)  # Rate limiting between tests
    
    # Test method 2
    result2 = test_geocoding_method_2()
    time.sleep(2)  # Rate limiting between tests
    
    # Test method 4
    result4 = test_geocoding_method_4()
    
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    if result1:
        print(f"Method 1 (geopy): SUCCESS - {result1}")
    else:
        print("Method 1 (geopy): FAILED")
    
    if result2:
        print(f"Method 2 (requests): SUCCESS - {result2}")
    else:
        print("Method 2 (requests): FAILED")
    
    if result4:
        print(f"Method 4 (simple test): SUCCESS - {result4}")
    else:
        print("Method 4 (simple test): FAILED")

if __name__ == "__main__":
    main()

