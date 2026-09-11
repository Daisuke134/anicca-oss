import SwiftUI

struct ContentView: View {
    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "sparkles")
                .font(.system(size: 44))
            Text("{{DISPLAY_NAME}}")
                .font(.title)
                .fontWeight(.semibold)
            Text("Your new product is ready for its first iteration.")
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
        }
        .padding()
    }
}

#Preview {
    ContentView()
}
